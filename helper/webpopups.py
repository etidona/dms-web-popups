#!/usr/bin/env python3
"""Web Popups helper.

Shows web apps in borderless layer-shell panels next to the DankMaterialShell
bar, rendered by Chromium (Qt WebEngine). One long-running daemon owns every
web view so pages can stay loaded in the background; the DMS plugin talks to
it through a Unix socket. The popup UI lives in Popup.qml.

Usage:
    webpopups.py send '<json command>'   # starts the daemon if needed
    webpopups.py daemon                   # run the daemon in the foreground
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
# Per-app settings the popup header can change, with their validators.
CHOOSABLE = {
    "size": lambda v: v in SIZES,
    "muted": lambda v: isinstance(v, bool),
}
UNREAD_RE = re.compile(r"\((\d+)\)|^\[(\d+)\]")
APP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EXTERNAL_SCHEMES = {"http", "https", "mailto"}
# Commands that make no sense without a running daemon: never spawn for them.
NO_SPAWN_COMMANDS = {"hide", "reload", "remove", "quit"}
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


def unique_path(folder, name):
    """Return folder/name, or folder/name (1).ext etc. when it already exists."""
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return candidate


def run_quiet(argv):
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


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

def try_send(sock_path, data):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock_path)
            s.sendall(data)
        return True
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
        return False


def send(payload):
    """Send one JSON command, starting the daemon when it is not running."""
    rdir = runtime_dir()
    sock_path = os.path.join(rdir, "ctl.sock")
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError:
        return 2
    if not isinstance(msg, dict):
        return 2
    data = payload.encode() + b"\n"
    if try_send(sock_path, data):
        return 0

    if msg.get("cmd") in NO_SPAWN_COMMANDS:
        if msg.get("cmd") == "remove" and msg.get("purge"):
            purge_app_data(msg.get("id"))
        return 0

    # Serialize spawning: many bar buttons register at the same moment.
    with open(os.path.join(rdir, "spawn.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if try_send(sock_path, data):
            return 0
        spawn_daemon(rdir)
        for _ in range(150):
            time.sleep(0.1)
            if try_send(sock_path, data):
                return 0
    print("webpopups: daemon did not start", file=sys.stderr)
    return 1


def spawn_daemon(rdir):
    log = open(os.path.join(rdir, "daemon.log"), "ab")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "daemon"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True)


# --------------------------------------------------------------------------
# daemon side
# --------------------------------------------------------------------------

def run_daemon():
    os.umask(0o077)
    rdir = runtime_dir()

    # Single instance: the lock is held for the daemon's whole lifetime.
    lock_file = open(os.path.join(rdir, "daemon.lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    # Native file dialogs through the desktop portal, unless a platform
    # theme is set already (DMS exports one, e.g. "kde").
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")

    from PyQt6.QtCore import (QMargins, QMetaObject, QObject, QSocketNotifier, QStandardPaths, Qt,
                              QTimer, QUrl, QVariant, Q_ARG, pyqtProperty, pyqtSlot)
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtQml import QQmlComponent, QQmlEngine
    from PyQt6.QtWebEngineQuick import QQuickWebEngineProfile, QtWebEngineQuick

    QtWebEngineQuick.initialize()
    # QApplication (not QGuiApplication): platform themes such as KDE's only
    # provide their native file dialog to widget applications.
    app = QApplication([sys.argv[0]])
    app.setApplicationName("webpopups")
    app.setQuitOnLastWindowClosed(False)

    sock_path = os.path.join(rdir, "ctl.sock")
    state_path = os.path.join(rdir, "state.json")

    def log(*args):
        print("webpopups:", *args, file=sys.stderr, flush=True)

    def call(obj, method, *args):
        QMetaObject.invokeMethod(obj, method, Qt.ConnectionType.DirectConnection,
                                 *[Q_ARG(QVariant, a) for a in args])

    class Backend(QObject):
        """Everything Popup.qml needs from the system, plus the daemon state."""

        def __init__(self):
            super().__init__()
            self.engine = QQmlEngine()
            self.engine.rootContext().setContextProperty("backend", self)
            self.engine.warnings.connect(lambda ws: [log(w.toString()) for w in ws])
            self.component = QQmlComponent(self.engine, QUrl.fromLocalFile(os.path.join(HERE, "Popup.qml")))
            if self.component.isError():
                for e in self.component.errors():
                    log(e.toString())
            self.popups = {}
            self.specs = {}
            self.unread = {}
            self.favicons = {}
            self.audible = {}
            self.autohidden_at = {}
            self.app_origins = {}
            self.theme = {}
            # Settings changed from the popup header ({app: {key: value}}),
            # until the plugin has saved them (they then come back in the spec).
            self.chosen = {}
            self.profiles = {}
            self.last_state = None
            self._icon_font = find_icon_font()
            self.write_state()

        # ---- properties and slots used by Popup.qml -----------------------

        @pyqtProperty(str, constant=True)
        def iconFontPath(self):
            return self._icon_font

        @pyqtSlot(str)
        def openExternal(self, url):
            open_uri(url)

        @pyqtSlot(str, str, str)
        def notify(self, app_id, title, body):
            spec = self.specs.get(app_id, {})
            name = clean_text(spec.get("name", "Web Popups"))
            run_quiet(["dms", "notify", "-a", name, "-i", self.favicons.get(app_id) or "web-browser",
                       "--", clean_text(title) or name, clean_text(body)])

        @pyqtSlot(str, str)
        def titleChanged(self, app_id, title):
            match = UNREAD_RE.search(title or "")
            unread = int(match.group(1) or match.group(2)) if match else 0
            if self.unread.get(app_id) != unread:
                self.unread[app_id] = unread
                self.write_state()

        @pyqtSlot(str, result=str)
        def faviconPath(self, app_id):
            if not valid_app_id(app_id):
                return ""
            return os.path.join(CACHE_DIR, app_id, "favicon.png")

        @pyqtSlot(str)
        def faviconSaved(self, app_id):
            path = self.faviconPath(app_id)
            if path and os.path.exists(path):
                self.favicons[app_id] = path
                self.write_state()

        @pyqtSlot(str, result=list)
        def downloadTarget(self, suggested):
            folder = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation) or os.path.expanduser("~/Downloads")
            os.makedirs(folder, exist_ok=True)
            path = unique_path(folder, safe_filename(suggested))
            return [folder, os.path.basename(path)]

        @pyqtSlot(str, str)
        def markAutohidden(self, app_id, screen):
            self.autohidden_at[app_id] = (time.monotonic(), screen)

        @pyqtSlot(str, str, result=bool)
        def recentlyAutohidden(self, app_id, screen):
            """True when the popup was auto-hidden by the very click that sent
            this toggle. A click on another monitor's bar still opens it there."""
            when, hidden_on = self.autohidden_at.get(app_id, (0.0, ""))
            if time.monotonic() - when >= AUTOHIDE_GRACE:
                return False
            return not (screen and hidden_on and screen != hidden_on)

        @pyqtSlot(str)
        def popupHidden(self, app_id):
            if not self.specs.get(app_id, {}).get("keepAlive", True):
                self.drop(app_id)

        @pyqtSlot(str, str, QVariant)
        def valueChosen(self, app_id, key, value):
            if key in CHOOSABLE and CHOOSABLE[key](value):
                self.chosen.setdefault(app_id, {})[key] = value
                self.write_state()

        @pyqtSlot(str, bool)
        def audibleChanged(self, app_id, audible):
            self.audible[app_id] = audible
            self.write_state()

        @pyqtSlot(QObject, int, int, int, int)
        def setMargins(self, window, left, top, right, bottom):
            for child in window.findChildren(QObject):
                if child.metaObject().className() == "LayerShellQt::Window":
                    child.setProperty("margins", QMargins(left, top, right, bottom))

        # ---- notification origins -----------------------------------------
        # Notifications are allowed for each app's own origins: the configured
        # URL and where it lands on the first load (redirects such as
        # youtube.com -> www.youtube.com), but only on the same site: a
        # redirect to a sign-in provider gets nothing. Saved so they
        # survive restarts.

        def origins_file(self, app_id):
            return os.path.join(DATA_DIR, app_id, "origins.json")

        def load_app_origins(self, app_id, url):
            origins = {origin_key(url)}
            try:
                with open(self.origins_file(app_id)) as f:
                    origins.update(o for o in json.load(f)
                                   if isinstance(o, str) and same_site(o, url))
            except (OSError, ValueError):
                pass
            origins.discard(None)
            self.app_origins[app_id] = origins

        @pyqtSlot(str, str)
        def addAppOrigin(self, app_id, uri):
            key = origin_key(uri)
            origins = self.app_origins.setdefault(app_id, set())
            app_url = self.specs.get(app_id, {}).get("url", "")
            if key is None or key in origins or not same_site(uri, app_url):
                return
            origins.add(key)
            try:
                os.makedirs(os.path.join(DATA_DIR, app_id), exist_ok=True)
                with open(self.origins_file(app_id), "w") as f:
                    json.dump(sorted(origins), f)
            except OSError:
                pass

        @pyqtSlot(str, result=list)
        def appOrigins(self, app_id):
            return sorted(self.app_origins.get(app_id, set()))

        @pyqtSlot(str, str, result=bool)
        def isAppOrigin(self, app_id, uri):
            return origin_key(uri) in self.app_origins.get(app_id, set())

        # ---- popups ---------------------------------------------------------

        def settle_chosen(self, spec):
            """Forget values chosen in the popup header once the plugin has
            saved them, so they can never override a later settings change."""
            chosen = self.chosen.get(spec["id"], {})
            for key in [k for k, v in chosen.items() if spec.get(k) == v]:
                del chosen[key]
            if not chosen:
                self.chosen.pop(spec["id"], None)
            self.write_state()

        def ensure(self, spec):
            app_id = spec["id"]
            spec = dict(spec, **self.chosen.get(app_id, {}))  # not saved by the plugin yet
            popup = self.popups.get(app_id)
            url_changed = self.specs.get(app_id, {}).get("url") != spec["url"]
            self.specs[app_id] = spec
            if popup is None:
                self.load_app_origins(app_id, spec["url"])
                popup = self.component.createWithInitialProperties({
                    "appSpec": spec,
                    "theme": self.theme,
                    "profile": self.profile_for(app_id),
                })
                if popup is None:
                    for e in self.component.errors():
                        log(e.toString())
                    raise RuntimeError("cannot create popup")
                self.popups[app_id] = popup
            else:
                popup.setProperty("appSpec", spec)
                if url_changed:
                    self.load_app_origins(app_id, spec["url"])
                    call(popup, "goHome")
            self.write_state()
            return popup

        def profile_for(self, app_id):
            """One disk profile per app, kept for the daemon's lifetime: a
            popup dropped and created again reuses it instead of opening a
            second profile on the same files."""
            profile = self.profiles.get(app_id)
            if profile is None:
                data_dir = os.path.join(DATA_DIR, app_id)
                cache_dir = os.path.join(CACHE_DIR, app_id)
                os.makedirs(data_dir, exist_ok=True)
                os.makedirs(cache_dir, exist_ok=True)
                profile = QQuickWebEngineProfile(self)
                # Paths before the name: otherwise Qt first opens the profile
                # in its default folder (~/.local/share/webpopups/QtWebEngine).
                profile.setPersistentStoragePath(data_dir)
                profile.setCachePath(cache_dir)
                profile.setStorageName("webpopups-" + app_id)
                profile.setOffTheRecord(False)
                profile.setPersistentCookiesPolicy(
                    QQuickWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
                profile.setPersistentPermissionsPolicy(
                    QQuickWebEngineProfile.PersistentPermissionsPolicy.StoreInMemory)
                # A plain Chrome user agent: some sites reject "QtWebEngine".
                profile.setHttpUserAgent(re.sub(r" QtWebEngine/\S+", "", profile.httpUserAgent()))
                self.profiles[app_id] = profile
            return profile

        def drop(self, app_id):
            popup = self.popups.pop(app_id, None)
            if popup is not None:
                popup.setProperty("visible", False)
                popup.deleteLater()
            self.unread.pop(app_id, None)
            self.audible.pop(app_id, None)
            self.write_state()

        def remove(self, app_id, purge):
            self.drop(app_id)
            profile = self.profiles.pop(app_id, None)
            if profile is not None:
                profile.clearHttpCache()
                # After the popup's view is gone; closing it closes its files.
                QTimer.singleShot(1000, profile.deleteLater)
            self.specs.pop(app_id, None)
            self.app_origins.pop(app_id, None)
            self.favicons.pop(app_id, None)
            self.chosen.pop(app_id, None)
            self.write_state()
            if purge:
                purge_app_data(app_id)
                if profile is not None:
                    # The profile may still write while it shuts down.
                    QTimer.singleShot(2000, lambda: purge_app_data(app_id))
                    QTimer.singleShot(6000, lambda: purge_app_data(app_id))

        def apply_theme(self, theme):
            if not isinstance(theme, dict) or theme == self.theme:
                return
            self.theme = theme
            for popup in self.popups.values():
                popup.setProperty("theme", theme)

        def handle(self, msg):
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
            app_id = msg.get("id")
            # A loaded popup takes new settings even without keepAlive.
            if cmd == "register" and spec and (spec.get("keepAlive", True) or spec["id"] in self.popups):
                self.ensure(spec)
            elif cmd == "toggle" and spec:
                call(self.ensure(spec), "toggle", place)
            elif cmd == "show" and spec:
                call(self.ensure(spec), "showAt", place)
            elif cmd == "hide" and app_id in self.popups:
                call(self.popups[app_id], "hidePopup")
            elif cmd == "reload" and app_id in self.popups:
                call(self.popups[app_id], "reloadPage")
            elif cmd == "remove" and valid_app_id(app_id):
                self.remove(app_id, bool(msg.get("purge")))
            elif cmd == "quit":
                app.quit()

        def write_state(self):
            state = {}
            for pid in set(self.popups) | set(self.favicons) | set(self.chosen):
                state[pid] = {"unread": self.unread.get(pid, 0),
                              "favicon": self.favicons.get(pid, "")}
                if pid in self.popups:
                    state[pid]["audible"] = self.audible.get(pid, False)
                    state[pid]["muted"] = bool(self.chosen.get(pid, {}).get(
                        "muted", self.specs.get(pid, {}).get("muted")))
                if pid in self.chosen:
                    state[pid]["chosen"] = self.chosen[pid]
            if state == self.last_state:
                return
            self.last_state = json.loads(json.dumps(state))
            tmp = state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, state_path)

    backend = Backend()

    # ---- socket ---------------------------------------------------------------
    # We hold daemon.lock, so any socket left here is stale.
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
        while True:
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

    notifier = QSocketNotifier(server.fileno(), QSocketNotifier.Type.Read)
    notifier.activated.connect(on_ready)

    app.exec()


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "daemon":
        run_daemon()
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        return send(sys.argv[2])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
