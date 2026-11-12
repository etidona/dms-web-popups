# DMS Web Popups

Open your web apps in a popup next to the DankMaterialShell bar, instead of in a
browser tab: WhatsApp, Telegram, Discord, Proton Mail, Claude, YouTube,
Syncthing, or any other site.

Each app gets its own bar button. Click it and the app slides out next to the
bar; click outside and it goes away, still signed in and still connected.

<img src="screenshot.png" alt="Wikipedia open in a Web Popups popup" width="420">

## Features

- **One button per app**: left click opens or closes the popup, right click
  reloads the page. Popups open on click only, even if your bar opens other
  popouts on hover.
- **Unread badges**: the number of unread messages appears on the bar button
  (read from the page title, e.g. "(3) WhatsApp").
- **Notifications**: site notifications go to the DMS notification center.
- **Stays connected**: apps can stay loaded in the background, so chats keep
  receiving messages while the popup is closed.
- **Separate logins**: every app has its own cookies and site data, kept
  across restarts.
- **Three sizes**, in proportion to the screen: small (a mini player, e.g.
  YouTube playback controls), medium and large. Change it from the settings or
  with the size button in the popup header.
- **Sound**: a small speaker on the bar button shows which app is playing
  sound, or that it is muted. Mute an app from the settings or with the mute
  button in the popup header.
- **Audio output per app**: send an app's sound to another output, e.g.
  YouTube to your headphones and WhatsApp to the speakers. It changes right
  away, without reloading the page.
- **Popup header**: back, reload, size, mute, audio output (when you have
  more than one), keep open (pin), plugin settings, close.
- **Grouped buttons** (optional): one button per bar that opens a panel with
  all your web apps when you hover over it, with the total unread count.
- **Fits your desktop**: follows the DMS theme colors, and works with bars on
  any screen edge and on multiple monitors.

## Usage

1. Open Settings → Plugins → Web Popups.
2. Click a preset, or add any site with a name, an `http`/`https` address and
   a [Material Symbols](https://fonts.google.com/icons) icon name.
3. The new button appears on your main bar, after your other Web Popups
   buttons (the first one in the center). Move it where you like from the bar
   settings.

Turn on **Group the app buttons** to keep the bar tidy: the first Web Popups
button of each bar shows a single globe icon. Hover over it and a panel
beside the bar shows all the apps of that bar. Click an app to open it, right
click to reload it; the gear in the panel, or a right click on the globe,
opens the plugin settings.

For each app you can choose:

- **Keep loaded in the background**: stays connected when closed, for
  notifications and unread badges. Turn it off for apps you only open now and
  then, to save memory: every loaded app takes about 250 MB.
- **Allow microphone** and **Allow camera**: needed for calls and voice
  messages. Both are off by default.
- **Mute audio**: silences the app's sounds. The mute button in the popup
  header changes the same setting.
- **Audio output**: where the app's sound goes. The audio output menu in the
  popup header changes the same setting.

## Privacy and security

- Microphone and camera are blocked unless you allow them for that app.
  Screen sharing is always blocked.
- Notifications are allowed only for the app's own site and its subdomains
  (e.g. `account.proton.me` for `mail.proton.me`). Sign-in providers on other
  domains, such as Google, get no permissions.
- Links that open a new window go to your default browser, and only when you
  click them. Only `http`, `https` and `mailto` links are passed on: pages
  cannot open local files or custom URL handlers.
- Pages cannot see your local network addresses through WebRTC.
- Downloads go to your Downloads folder, with a safe file name that never
  overwrites an existing file.
- Removing an app from the settings also deletes its login, history and site
  data.

## Requirements

- DankMaterialShell 2.0 or later
- A Wayland compositor with layer-shell support (niri, Hyprland, Sway, KDE
  Plasma, ...)
- Python 3 with PyQt6 and Qt WebEngine, the layer-shell-qt QML module,
  `xdg-open` (xdg-utils) for links, and `pactl` for the per-app audio output
  (PipeWire with pipewire-pulse, or PulseAudio):

| Distro | Command |
|---|---|
| Fedora | `sudo dnf install python3-pyqt6-webengine layer-shell-qt xdg-utils pulseaudio-utils` |
| Arch | `sudo pacman -S python-pyqt6-webengine layer-shell-qt xdg-utils libpulse` |

On other distros, install the equivalent packages. The plugin checks the
requirements when you enable it and tells you if something is missing.

Tested on Fedora Asahi Linux (Apple Silicon MacBook) with niri.

## Install

```
git clone https://github.com/etidona/dms-web-popups.git ~/.config/DankMaterialShell/plugins/webPopups
```

Then enable it in Settings → Plugins and add your first app.

## How it works

Pages are rendered by Chromium (Qt WebEngine). Qt WebEngine cannot run inside
Quickshell, so helper processes show the popups as layer-shell surfaces, the
same kind of window the bar itself uses. Every app runs in a helper process
of its own, started the first time it is needed, so it can stay loaded in
the background. One process per app also keeps the apps'
sound apart: Chromium does not tell apart the audio of the pages it plays,
but every stream of a helper carries `application.id=webpopups.<id>`. The
helper moves them to the app's output with `pactl`, and WirePlumber
remembers that output for the app. The bar buttons talk to the helpers
through Unix sockets, and stop them when the plugin is disabled.

- `WebPopupsWidget.qml`: the bar button
- `WebPopupsSettings.qml`: the settings page
- `helper/webpopups.py`: the helper
- `helper/Popup.qml`: the popup

Where the data is kept (`<id>` is the id DMS gives each app, e.g.
`variant_1794317955129`):

- Logins and site data: `~/.local/share/webpopups/<id>`
- Cache: `~/.cache/webpopups/<id>`
- Sockets, state and log: `$XDG_RUNTIME_DIR/webpopups/`

## Limitations

- DRM-protected media (Spotify, Netflix, Prime Video) does not play: Qt
  WebEngine ships without Widevine.
- Some sites, especially Google sign-in, may refuse embedded browsers. The
  helper identifies itself as plain Chrome, which works for most sites.
- Sign-in flows that open a separate window open in your browser instead, and
  may not complete there.
- Fullscreen videos fill the popup, not the whole screen.

## Uninstall

Remove your apps from the plugin settings first, to delete their logins and
site data. Then disable the plugin and delete its folder:

```
rm -rf ~/.config/DankMaterialShell/plugins/webPopups
```

Any data left behind is in `~/.local/share/webpopups` and
`~/.cache/webpopups`.

---

Built with AI assistance; design, testing and maintenance by me.
