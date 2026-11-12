#!/usr/bin/env python3
"""Web Popups helper.

Shows web apps in borderless layer-shell panels next to the DankMaterialShell
bar, rendered by Chromium (Qt WebEngine). Every app runs in a long-running
daemon of its own, so its page can stay loaded in the background; the DMS
plugin talks to the daemons through Unix sockets. The popup UI lives in
Popup.qml.

One process per app also keeps the apps' sound apart: Chromium does not tell
its pages' audio streams apart, but every stream of a daemon carries
application.id "webpopups.<app id>". The daemon moves them to the app's
chosen output with pactl, and WirePlumber, which remembers outputs per
application.id, starts the app's later streams there.

Usage:
    webpopups.py send '<json command>'   # starts the app's daemon if needed
    webpopups.py daemon <app id>          # run a daemon in the foreground
    webpopups.py purge <app id>           # delete an app's data (after 3 s)
"""
import fcntl
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_HOME = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
CACHE_HOME = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
DATA_DIR = os.path.join(DATA_HOME, "webpopups")
CACHE_DIR = os.path.join(CACHE_HOME, "webpopups")

# Seconds: a toggle right after a focus-loss hide keeps the popup hidden
# (clicking the bar button first takes focus away, then sends the toggle).
AUTOHIDE_GRACE = 0.6
SIZES = ("small", "medium", "large")
# Audio output (sink) names, e.g. "alsa_output.pci-0000_00_1f.3.analog-stereo":
# any printable text, but never a leading "-" that pactl would read as an option.
SINK_RE = re.compile(r"^(?!-)[^\x00-\x1f\x7f]{1,256}$")
# Per-app settings the popup header can change, with their validators.
CHOOSABLE = {
    "size": lambda v: v in SIZES,
    "muted": lambda v: isinstance(v, bool),
    "audioOutput": lambda v: v == "" or (isinstance(v, str) and bool(SINK_RE.match(v))),
}
UNREAD_RE = re.compile(r"\((\d+)\)|^\[(\d+)\]")
APP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EXTERNAL_SCHEMES = {"http", "https", "mailto"}
# Commands that make no sense without a running daemon: never spawn for them.
NO_SPAWN_COMMANDS = {"hide", "reload"}
MAX_COMMAND_BYTES = 1 << 20

ICON_FONT_GLOBS = [
    "/usr/share/quickshell/dms/**/MaterialSymbolsRounded*.ttf",
]


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        sys.exit("webpopups: XDG_RUNTIME_DIR is not set")
    path = os.path.join(base, "webpopups")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def valid_app_id(app_id):
    return isinstance(app_id, str) and bool(APP_ID_RE.match(app_id))


def audio_output(spec):
    out = spec.get("audioOutput") or ""
    return out if isinstance(out, str) and SINK_RE.match(out) else ""


def socket_path(rdir, app_id):
    return os.path.join(rdir, f"ctl-{app_id}.sock")


def lock_path(rdir, app_id):
    return os.path.join(rdir, f"daemon-{app_id}.lock")


def removed_marker(rdir, app_id):
    """Set when an app is removed, for the rest of the session: a late click
    from another bar must not start it again (and create its profile)."""
    return os.path.join(rdir, "removed", app_id)


def all_sockets(rdir):
    # Also matches ctl.sock, the single daemon of versions before 0.4.0, so
    # that "quit" stops it after an update.
    return sorted(glob.glob(os.path.join(rdir, "ctl*.sock")))


def daemon_running(rdir, app_id):
    """True while a daemon of the app holds its lock (also while starting)."""
    try:
        with open(lock_path(rdir, app_id), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def merge_state(rdir):
    """Write state.json, read by the bar buttons, from the state of every
    running daemon (state.d/<app id>.json). Files of daemons that are no
    longer running (e.g. crashed) are deleted. Apps without a running daemon
    keep their saved favicon."""
    state_dir = os.path.join(rdir, "state.d")
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(rdir, "state.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = {}
        for icon in glob.glob(os.path.join(CACHE_DIR, "*", "favicon.png")):
            app_id = os.path.basename(os.path.dirname(icon))
            if valid_app_id(app_id):
                merged[app_id] = {"unread": 0, "favicon": icon}
        for path in sorted(glob.glob(os.path.join(state_dir, "*.json"))):
            app_id = os.path.basename(path)[:-5]
            if not daemon_running(rdir, app_id):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            try:
                with open(path) as f:
                    part = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(part, dict):
                continue
            for key, entry in part.items():
                if isinstance(entry, dict):
                    if not entry.get("favicon") and key in merged:
                        entry["favicon"] = merged[key]["favicon"]
                    merged[key] = entry
        target = os.path.join(rdir, "state.json")
        try:
            with open(target) as f:
                if json.load(f) == merged:
                    return
        except (OSError, ValueError):
            pass
        tmp = target + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f)
        os.replace(tmp, target)


def purge_app_data(app_id):
    """Delete the logins, site data and cache of one app."""
    if not valid_app_id(app_id):
        return
    for path in (os.path.join(DATA_DIR, app_id),
                 os.path.join(CACHE_DIR, app_id),
                 # Left by earlier versions, which opened the profile in
                 # Qt's default folder before setting its paths.
                 os.path.join(DATA_DIR, "QtWebEngine", "webpopups-" + app_id)):
        shutil.rmtree(path, ignore_errors=True)


def origin_key(uri):
    """Return "scheme://host[:port]" for a web URI, or None."""
    parts = urlsplit(uri or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


DEFAULT_PORTS = {"http": 80, "https": 443}


def site_domain(host):
    """The domain a host shares with its sibling hosts: "mail.proton.me" ->
    "proton.me". Two-label hosts stand for themselves, and so do three-label
    hosts under a country domain like "example.co.uk". (No public suffix
    list: "a.github.io" -> "github.io" still counts as one site.)"""
    labels = host.split(".")
    if len(labels) <= 2 or (len(labels) == 3 and len(labels[2]) == 2 and len(labels[1]) <= 3):
        return host
    return ".".join(labels[1:])


def same_site(uri, app_url):
    """True if uri is on the app's domain (the app's host, its subdomains and
    its sibling subdomains). Keeps sign-in providers out. Local addresses
    (IPs, localhost) must match host and port exactly."""
    try:
        u, a = urlsplit(uri or ""), urlsplit(app_url or "")
        host, base = u.hostname, a.hostname
        if not host or not base:
            return False
        if base == "localhost" or re.fullmatch(r"[0-9.]+|[0-9a-f:]+", base):
            return host == base and \
                (u.port or DEFAULT_PORTS.get(u.scheme)) == (a.port or DEFAULT_PORTS.get(a.scheme))
    except ValueError:  # invalid port
        return False
    domain = site_domain(base)
    return host == domain or host.endswith("." + domain)


def clean_text(text):
    """Strip control characters from site-provided text."""
    return "".join(ch for ch in (text or "") if ch.isprintable() or ch == "\n")[:1000]


def safe_filename(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch.isprintable())
    if name in ("", ".", ".."):
        name = "download"
    name = name.lstrip(".") or "download"
    stem, ext = os.path.splitext(name)
    while len((stem + ext).encode()) > 200 and stem:
        stem = stem[:-1]
    return (stem + ext) if stem else "download" + ext[:20]


def unique_path(folder, name, reserved=()):
    """Return folder/name, or folder/name (1).ext etc. when it already exists
    or is reserved by a download in progress."""
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    n = 1
    while os.path.exists(candidate) or candidate in reserved:
        candidate = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return candidate


def child_env():
    """The environment for programs the daemon starts: without the app's
    audio tag, or a browser opened from a link would get it (and its sound
    would be routed with the app's)."""
    return {k: v for k, v in os.environ.items() if k != "PULSE_PROP"}


def run_quiet(argv):
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=child_env())
    except OSError:
        pass


def pactl_json(*args):
    out = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, timeout=3,
                         env=child_env())
    return json.loads(out.stdout or b"[]")


def open_uri(uri):
    """Open a link in the default app, only for web and mail links: pages
    must not be able to launch local files or custom URL handlers."""
    if uri and urlsplit(uri).scheme.lower() in EXTERNAL_SCHEMES:
        run_quiet(["xdg-open", uri])


def find_icon_font():
    for pattern in ICON_FONT_GLOBS:
        for path in glob.glob(pattern, recursive=True):
            return path
    return ""


# --------------------------------------------------------------------------
# client side
# --------------------------------------------------------------------------

SOCKET_ERRORS = (FileNotFoundError, ConnectionRefusedError, socket.timeout)


def try_send(sock_path, data):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock_path)
            s.sendall(data)
        return True
    except SOCKET_ERRORS:
        return False


def send(payload):
    """Send one JSON command to the daemon of its app, starting the daemon
    when it is not running. "quit" goes to every daemon."""
    rdir = runtime_dir()
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError:
        return 2
    if not isinstance(msg, dict):
        return 2
    data = payload.encode() + b"\n"
    cmd = msg.get("cmd")

    if cmd == "quit":
        # Also daemons still starting up: they have the lock, not yet a socket.
        sent = set()
        deadline = time.monotonic() + 5
        while True:
            for path in all_sockets(rdir):
                if path not in sent and try_send(path, data):
                    sent.add(path)
            starting = [lock for lock in glob.glob(os.path.join(rdir, "daemon-*.lock"))
                        if socket_path(rdir, os.path.basename(lock)[7:-5]) not in sent
                        and daemon_running(rdir, os.path.basename(lock)[7:-5])]
            if not starting or time.monotonic() > deadline:
                return 0
            time.sleep(0.2)

    spec = msg.get("app")
    app_id = spec.get("id") if isinstance(spec, dict) else msg.get("id")
    if not valid_app_id(app_id):
        return 2
    sock_path = socket_path(rdir, app_id)
    if cmd == "remove":
        os.makedirs(os.path.dirname(removed_marker(rdir, app_id)), exist_ok=True)
        open(removed_marker(rdir, app_id), "w").close()
        if not try_send(sock_path, data) and msg.get("purge"):
            purge_app_data(app_id)
        return 0
    if os.path.exists(removed_marker(rdir, app_id)):
        return 0
    if try_send(sock_path, data):
        return 0
    merge_state(rdir)  # the daemon may have crashed: drop its state
    if cmd in NO_SPAWN_COMMANDS or not isinstance(spec, dict):
        return 0
    if cmd == "register" and not spec.get("keepAlive", True):
        return 0  # nothing to load until the popup is opened

    # Several bars register the same app at the same moment.
    with open(os.path.join(rdir, f"spawn-{app_id}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if try_send(sock_path, data):
            return 0
        spawn_daemon(rdir, app_id)
        for _ in range(150):
            time.sleep(0.1)
            if try_send(sock_path, data):
                return 0
    print("webpopups: daemon did not start", file=sys.stderr)
    return 1


def spawn_daemon(rdir, app_id):
    log = open(os.path.join(rdir, "daemon.log"), "ab")
    # Tags every audio stream of this daemon (see AudioRouter).
    env = dict(os.environ, PULSE_PROP=f"application.id='webpopups.{app_id}'")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "daemon", app_id],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                     start_new_session=True)


# --------------------------------------------------------------------------
# daemon side
# --------------------------------------------------------------------------

def run_daemon(own_app):
    os.umask(0o077)
    rdir = runtime_dir()

    # One daemon per app. The lock is held until the process exits; a daemon
    # of the same app that is still shutting down holds it a little longer.
    lock_file = open(lock_path(rdir, own_app), "w")
    for _ in range(100):
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            time.sleep(0.1)
    else:
        return

    # Native file dialogs through the desktop portal, unless a platform
    # theme is set already (DMS exports one, e.g. "kde").
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")

    from PyQt6.QtCore import (QCoreApplication, QEvent, QMargins, QMetaObject, QObject,
                              QSocketNotifier, QStandardPaths, Qt, QTimer, QUrl, QVariant, Q_ARG,
                              pyqtProperty, pyqtSlot)
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtQml import QQmlComponent, QQmlEngine
    from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest
    from PyQt6.QtWebEngineQuick import QQuickWebEngineProfile, QtWebEngineQuick

    QtWebEngineQuick.initialize()
    # QApplication (not QGuiApplication): platform themes such as KDE's only
    # provide their native file dialog to widget applications.
    app = QApplication([sys.argv[0]])
    app.setApplicationName("webpopups")
    app.setQuitOnLastWindowClosed(False)

    sock_path = socket_path(rdir, own_app)
    os.makedirs(os.path.join(rdir, "state.d"), exist_ok=True)
    state_path = os.path.join(rdir, "state.d", own_app + ".json")

    def log(*args):
        print(f"webpopups[{own_app}]:", *args, file=sys.stderr, flush=True)

    def call(obj, method, *args):
        QMetaObject.invokeMethod(obj, method, Qt.ConnectionType.DirectConnection,
                                 *[Q_ARG(QVariant, a) for a in args])

    class AudioRouter(QObject):
        """Sends the app's audio streams (tagged with application.id, see
        spawn_daemon) to its chosen output: the streams playing now, and
        every new one that starts elsewhere. WirePlumber starts new streams
        on the output it remembers for the app, so moves are rare.
        pactl runs in a worker thread, never in the GUI thread."""

        def __init__(self):
            super().__init__()
            self.tag = "webpopups." + own_app
            self.target = None  # unknown until the first spec
            self.proc = None
            self.notifier = None
            self.stopped = False
            self.lock = threading.Lock()
            self.running = False
            self.again = False
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.setInterval(100)
            self.timer.timeout.connect(self.start_apply)
            # pactl subscribe ends when the sound server restarts: listen again.
            self.retry = QTimer(self)
            self.retry.setSingleShot(True)
            self.retry.setInterval(5000)
            self.retry.timeout.connect(self.listen)

        def set_target(self, sink):
            if sink == self.target:
                return
            self.target = sink
            self.listen()
            self.timer.start()

        def listen(self):
            if self.stopped or self.proc is not None:
                return
            try:
                self.proc = subprocess.Popen(["pactl", "subscribe"], stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             env=dict(child_env(), LC_ALL="C"))  # untranslated events
            except OSError as exc:
                log("cannot route audio, pactl is missing:", exc)
                return
            fd = self.proc.stdout.fileno()
            os.set_blocking(fd, False)
            self.notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
            self.notifier.activated.connect(self.on_event)
            self.timer.start()  # streams may have started while not listening

        def on_event(self, *_):
            if self.proc is None:
                return
            try:
                data = os.read(self.proc.stdout.fileno(), 65536)
            except BlockingIOError:
                return
            if not data:
                self.close_proc()
                if not self.stopped:
                    self.retry.start()
                return
            # A new stream, or an output that appeared (e.g. headphones).
            if b"'new' on sink-input" in data or b"'new' on sink #" in data:
                self.timer.start()

        def close_proc(self):
            if self.notifier is not None:
                self.notifier.setEnabled(False)
                self.notifier = None
            if self.proc is not None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
                self.proc = None

        def start_apply(self):
            if self.target is None or self.stopped:
                return
            with self.lock:
                if self.running:
                    self.again = True
                    return
                self.running = True
            threading.Thread(target=self.work, daemon=True).start()

        def work(self):
            while True:
                self.apply(self.target)
                with self.lock:
                    if not self.again or self.stopped:
                        self.running = False
                        return
                    self.again = False

        def apply(self, target):
            try:
                streams = [st for st in pactl_json("list", "sink-inputs")
                           if (st.get("properties") or {}).get("application.id") == self.tag]
                if not streams:
                    return
                sinks = {sk.get("name"): sk.get("index") for sk in pactl_json("list", "short", "sinks")}
                if target:
                    if target not in sinks:
                        return  # not connected: the default output plays it
                    want, move_to = sinks[target], target
                else:
                    # Back to the default output. A move to @DEFAULT_SINK@
                    # also makes WirePlumber forget the app's output, so its
                    # streams follow the default output again.
                    default = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                             text=True, timeout=3, env=child_env()).stdout.strip()
                    want, move_to = sinks.get(default), "@DEFAULT_SINK@"
                for st in streams:
                    if st.get("sink") != want:
                        subprocess.run(["pactl", "move-sink-input", str(st["index"]), move_to],
                                       capture_output=True, timeout=3, env=child_env())
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                log("audio routing failed:", exc)

        def stop(self):
            self.stopped = True
            self.timer.stop()
            self.retry.stop()
            self.close_proc()

    class Backend(QObject):
        """Everything Popup.qml needs from the system, plus the state of the
        daemon's one app. The slots take the app id that Popup.qml passes,
        always this daemon's app."""

        def __init__(self):
            super().__init__()
            self.engine = QQmlEngine()
            self.engine.rootContext().setContextProperty("backend", self)
            self.engine.warnings.connect(lambda ws: [log(w.toString()) for w in ws])
            self.component = QQmlComponent(self.engine, QUrl.fromLocalFile(os.path.join(HERE, "Popup.qml")))
            if self.component.isError():
                for e in self.component.errors():
                    log(e.toString())
            self.spec = None
            self.popup = None
            self.profile = None
            self.unread = 0
            self.audible = False
            icon = self.faviconPath(own_app)
            self.favicon = icon if os.path.exists(icon) else ""
            self.autohidden = (0.0, "")
            self.origins = set()
            self.theme = {}
            # Settings changed from the popup header, until the plugin has
            # saved them (they then come back in the spec).
            self.chosen = {}
            self.removed = False
            self.purge = False
            # Downloads in progress, and the file names they will write.
            self.downloads = set()
            self.reserved = set()
            self.last_state = None
            self.closing = False
            self.router = AudioRouter()
            self._icon_font = find_icon_font()
            self.write_state()

        # ---- properties and slots used by Popup.qml -----------------------

        @pyqtProperty(str, constant=True)
        def iconFontPath(self):
            return self._icon_font

        @pyqtSlot(str)
        def openExternal(self, url):
            open_uri(url)

        @pyqtSlot()
        def openSettings(self):
            run_quiet(["dms", "ipc", "call", "settings", "openWith", "plugin:webPopups"])

        @pyqtSlot(str, str, str)
        def notify(self, app_id, title, body):
            name = clean_text((self.spec or {}).get("name", "Web Popups"))
            run_quiet(["dms", "notify", "-a", name, "-i", self.favicon or "web-browser",
                       "--", clean_text(title) or name, clean_text(body)])

        @pyqtSlot(str, str)
        def titleChanged(self, app_id, title):
            match = UNREAD_RE.search(title or "")
            unread = int(match.group(1) or match.group(2)) if match else 0
            if self.unread != unread:
                self.unread = unread
                self.write_state()

        @pyqtSlot(str, result=str)
        def faviconPath(self, app_id):
            return os.path.join(CACHE_DIR, own_app, "favicon.png")

        @pyqtSlot(str)
        def faviconSaved(self, app_id):
            path = self.faviconPath(own_app)
            if os.path.exists(path):
                self.favicon = path
                self.write_state()

        @pyqtSlot(str, QObject)
        def startDownload(self, app_id, download):
            """Save a download in the Downloads folder under a safe, unused
            name, and notify when it ends. Tracked here, not in the popup,
            so it completes even if the popup is dropped first: the daemon
            does not quit while a download is running."""
            if not isinstance(download, QWebEngineDownloadRequest):
                log("unexpected download object:", type(download).__name__)
                return
            folder = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation) or os.path.expanduser("~/Downloads")
            os.makedirs(folder, exist_ok=True)
            path = unique_path(folder, safe_filename(download.downloadFileName()), self.reserved)
            self.reserved.add(path)
            self.downloads.add(download)
            name = os.path.basename(path)

            def finished():
                if not download.isFinished():
                    return
                self.reserved.discard(path)
                self.downloads.discard(download)
                if download.state() == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                    self.notify(own_app, "Download complete", name)
                else:
                    self.notify(own_app, "Download failed", download.interruptReasonString())
                self.quit_when_idle()

            download.isFinishedChanged.connect(finished)
            download.setDownloadDirectory(folder)
            download.setDownloadFileName(name)
            download.accept()

        @pyqtSlot(str, str)
        def markAutohidden(self, app_id, screen):
            self.autohidden = (time.monotonic(), screen)

        @pyqtSlot(str, str, result=bool)
        def recentlyAutohidden(self, app_id, screen):
            """True when the popup was auto-hidden by the very click that sent
            this toggle. A click on another monitor's bar still opens it there."""
            when, hidden_on = self.autohidden
            if time.monotonic() - when >= AUTOHIDE_GRACE:
                return False
            return not (screen and hidden_on and screen != hidden_on)

        @pyqtSlot(str)
        def popupHidden(self, app_id):
            if not (self.spec or {}).get("keepAlive", True):
                self.drop()

        @pyqtSlot(str, str, QVariant)
        def valueChosen(self, app_id, key, value):
            if key not in CHOOSABLE or not CHOOSABLE[key](value):
                return
            self.chosen[key] = value
            if self.spec is not None:
                self.spec[key] = value
            if key == "audioOutput":
                # Right away, not when the plugin sends the saved setting back.
                self.router.set_target(value)
                if self.popup is not None:
                    self.popup.setProperty("appSpec", dict(self.spec))
            self.write_state()

        @pyqtSlot(str, bool)
        def audibleChanged(self, app_id, audible):
            self.audible = audible
            self.write_state()

        @pyqtSlot(QObject, int, int, int, int)
        def setMargins(self, window, left, top, right, bottom):
            for child in window.findChildren(QObject):
                if child.metaObject().className() == "LayerShellQt::Window":
                    child.setProperty("margins", QMargins(left, top, right, bottom))

        # ---- notification origins -----------------------------------------
        # Notifications are allowed for the app's own origins: the configured
        # URL and where it lands on the first load (redirects such as
        # youtube.com -> www.youtube.com), but only on the same site: a
        # redirect to a sign-in provider gets nothing. Saved so they
        # survive restarts.

        def origins_file(self):
            return os.path.join(DATA_DIR, own_app, "origins.json")

        def load_origins(self, url):
            origins = {origin_key(url)}
            try:
                with open(self.origins_file()) as f:
                    origins.update(o for o in json.load(f)
                                   if isinstance(o, str) and same_site(o, url))
            except (OSError, ValueError):
                pass
            origins.discard(None)
            self.origins = origins

        @pyqtSlot(str, str)
        def addAppOrigin(self, app_id, uri):
            key = origin_key(uri)
            app_url = (self.spec or {}).get("url", "")
            if key is None or key in self.origins or not same_site(uri, app_url):
                return
            self.origins.add(key)
            try:
                os.makedirs(os.path.join(DATA_DIR, own_app), exist_ok=True)
                with open(self.origins_file(), "w") as f:
                    json.dump(sorted(self.origins), f)
            except OSError:
                pass

        @pyqtSlot(str, result=list)
        def appOrigins(self, app_id):
            return sorted(self.origins)

        @pyqtSlot(str, str, result=bool)
        def isAppOrigin(self, app_id, uri):
            return origin_key(uri) in self.origins

        # ---- the popup ------------------------------------------------------

        def settle_chosen(self, spec):
            """Forget values chosen in the popup header once the plugin has
            saved them, so they can never override a later settings change."""
            for key in [k for k, v in self.chosen.items() if spec.get(k) == v]:
                del self.chosen[key]
            self.write_state()

        def ensure(self, spec):
            if spec["id"] != own_app:
                raise ValueError("not this daemon's app: " + spec["id"])
            if self.removed:
                raise ValueError("app was removed")
            spec = dict(spec, **self.chosen)  # not saved by the plugin yet
            url_changed = self.spec is not None and self.spec.get("url") != spec["url"]
            self.spec = spec
            self.router.set_target(audio_output(spec))
            if self.popup is None:
                self.load_origins(spec["url"])
                popup = self.component.createWithInitialProperties({
                    "appSpec": spec,
                    "theme": self.theme,
                    "profile": self.profile_for(),
                })
                if popup is None:
                    for e in self.component.errors():
                        log(e.toString())
                    raise RuntimeError("cannot create popup")
                self.popup = popup
            else:
                self.popup.setProperty("appSpec", spec)
                if url_changed:
                    self.load_origins(spec["url"])
                    call(self.popup, "goHome")
            self.write_state()
            return self.popup

        def profile_for(self):
            """The app's disk profile, kept for the daemon's lifetime: a popup
            dropped and created again reuses it instead of opening a second
            profile on the same files."""
            if self.profile is None:
                data_dir = os.path.join(DATA_DIR, own_app)
                cache_dir = os.path.join(CACHE_DIR, own_app)
                os.makedirs(data_dir, exist_ok=True)
                os.makedirs(cache_dir, exist_ok=True)
                profile = QQuickWebEngineProfile(self)
                # Paths before the name: otherwise Qt first opens the profile
                # in its default folder (~/.local/share/webpopups/QtWebEngine).
                profile.setPersistentStoragePath(data_dir)
                profile.setCachePath(cache_dir)
                profile.setStorageName("webpopups-" + own_app)
                profile.setOffTheRecord(False)
                profile.setPersistentCookiesPolicy(
                    QQuickWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
                profile.setPersistentPermissionsPolicy(
                    QQuickWebEngineProfile.PersistentPermissionsPolicy.StoreInMemory)
                # A plain Chrome user agent: some sites reject "QtWebEngine".
                profile.setHttpUserAgent(re.sub(r" QtWebEngine/\S+", "", profile.httpUserAgent()))
                profile.presentNotification.connect(
                    lambda n: self.notify(own_app, n.title(), n.message()))
                self.profile = profile
            return self.profile

        def drop(self):
            """Unload the page (the profile stays); the daemon then quits."""
            if self.popup is not None:
                self.popup.setProperty("visible", False)
                self.popup.deleteLater()
                self.popup = None
            self.unread = 0
            self.audible = False
            self.write_state()
            self.quit_when_idle()

        def quit_when_idle(self):
            """Quit once no popup is loaded and no download is running."""
            QTimer.singleShot(1500, self.quit_if_idle)

        def quit_if_idle(self):
            if not self.closing and self.popup is None and not self.downloads:
                self.shutdown()

        def remove(self, purge):
            self.removed = True
            for download in list(self.downloads):
                download.cancel()
            self.chosen = {}
            self.favicon = ""
            if purge:
                self.purge = True
                purge_app_data(own_app)
            self.drop()

        def apply_theme(self, theme):
            if not isinstance(theme, dict) or theme == self.theme:
                return
            self.theme = theme
            if self.popup is not None:
                self.popup.setProperty("theme", theme)

        def handle(self, msg):
            if self.closing:
                return
            if not isinstance(msg, dict):
                raise ValueError("command must be a JSON object")
            cmd = msg.get("cmd")
            if "theme" in msg:
                self.apply_theme(msg["theme"])
            spec = msg.get("app")
            if spec is not None:
                if not isinstance(spec, dict) or not valid_app_id(spec.get("id")) or \
                        urlsplit(spec.get("url", "")).scheme not in ("http", "https"):
                    raise ValueError("invalid app spec")
                self.settle_chosen(spec)
            place = msg.get("place") or {}
            # A loaded popup takes new settings even without keepAlive.
            if cmd == "register" and spec and (spec.get("keepAlive", True) or self.popup is not None):
                self.ensure(spec)
            elif cmd == "toggle" and spec:
                call(self.ensure(spec), "toggle", place)
            elif cmd == "show" and spec:
                call(self.ensure(spec), "showAt", place)
            elif cmd == "hide" and self.popup is not None:
                call(self.popup, "hidePopup")
            elif cmd == "reload" and self.popup is not None:
                call(self.popup, "reloadPage")
            elif cmd == "remove" and msg.get("id") == own_app:
                self.remove(bool(msg.get("purge")))
            elif cmd == "quit":
                self.shutdown()

        def shutdown(self):
            """Quit without crashing: stop taking commands, delete the popup
            first, then the profile it uses, then the QML engine. Runs once,
            however many quit commands arrive. The daemon lock stays held
            until the process exits, so a new daemon of this app waits."""
            if self.closing:
                return
            self.closing = True
            server_notifier.setEnabled(False)
            server.close()
            for path in (sock_path, state_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            merge_state(rdir)
            self.router.stop()
            if self.popup is not None:
                self.popup.setProperty("visible", False)
                self.popup.deleteLater()
                self.popup = None
            app.processEvents()
            if self.profile is not None:
                self.profile.deleteLater()
                self.profile = None
            app.processEvents()
            self.component.deleteLater()
            self.engine.deleteLater()
            app.processEvents()
            app.quit()

        def write_state(self):
            state = {}
            if not self.removed and (self.popup is not None or self.favicon or self.chosen):
                entry = {"unread": self.unread, "favicon": self.favicon}
                if self.popup is not None:
                    entry["audible"] = self.audible
                    entry["muted"] = bool((self.spec or {}).get("muted"))
                if self.chosen:
                    entry["chosen"] = self.chosen
                state[own_app] = entry
            if state == self.last_state:
                return
            self.last_state = json.loads(json.dumps(state))
            tmp = state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, state_path)
            merge_state(rdir)

    backend = Backend()

    # ---- socket ---------------------------------------------------------------
    # We hold this app's daemon lock, so a socket left here is stale.
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(16)
    server.setblocking(False)

    def read_command(conn):
        conn.settimeout(0.5)
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < MAX_COMMAND_BYTES:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf

    def on_ready(*_):
        # Never let an exception escape: keep serving later commands.
        while not backend.closing:
            try:
                conn, _ = server.accept()
            except BlockingIOError:
                return
            except Exception as exc:
                log("accept failed:", exc)
                return
            try:
                with conn:
                    buf = read_command(conn)
                for line in buf.decode(errors="replace").splitlines():
                    if line.strip():
                        try:
                            backend.handle(json.loads(line))
                        except Exception as exc:
                            log("command failed:", exc)
            except Exception as exc:
                log("connection failed:", exc)

    server_notifier = QSocketNotifier(server.fileno(), QSocketNotifier.Type.Read)
    server_notifier.activated.connect(on_ready)
    # Started for a command that loads nothing (or that failed): do not stay.
    backend.quit_when_idle()

    app.exec()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    if backend.purge:
        # Chromium may still write the profile while this process exits:
        # delete the app's data once more from outside, a little later.
        run_quiet([sys.executable, os.path.abspath(__file__), "purge", own_app])


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "daemon":
        if len(sys.argv) < 3 or not valid_app_id(sys.argv[2]):
            return 2
        run_daemon(sys.argv[2])
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        return send(sys.argv[2])
    if len(sys.argv) >= 3 and sys.argv[1] == "purge":
        time.sleep(3)
        purge_app_data(sys.argv[2])  # checks the id
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
