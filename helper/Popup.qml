// One web app popup: a layer-shell panel with a header and a Chromium view.
// Created and driven by webpopups.py; `backend` is the Python side.
import QtQuick
import QtQuick.Dialogs
import QtWebEngine
import org.kde.layershell 1.0 as LayerShell

Window {
    id: win

    required property var appSpec
    // Created by webpopups.py, one per app, reused when the popup is
    // created again (see Backend.profile_for).
    required property WebEngineProfile profile
    property var theme: ({})

    readonly property string appId: appSpec.id
    property string currentSize: appSpec.size || "medium"
    property bool muted: !!appSpec.muted
    readonly property string audioOutput: appSpec.audioOutput || ""
    // Audio outputs, [{name, label}], sent by the plugin with the theme.
    readonly property var outputs: theme.outputs || []
    property bool pinned: false
    property bool wasActive: false
    property bool dialogOpen: false
    property bool initialLoad: true
    property string screenName: ""
    property var lastPlace: null
    property int anchorBits: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorRight

    // [width, height] as fractions of the screen, so sizes scale with the
    // monitor. "small" is a mini player (e.g. YouTube controls).
    readonly property var sizes: ({
        small: [0.25, 0.35],
        medium: [0.34, 0.80],
        large: [0.75, 0.94]
    })
    readonly property var sizeOrder: Object.keys(sizes)

    readonly property color bgColor: theme.bg || "#1d1b20"
    readonly property color fgColor: theme.fg || "#e6e0e9"
    readonly property color accentColor: theme.accent || "#d0bcff"
    readonly property color borderColor: theme.border || "#40ffffff"
    readonly property int radius: theme.radius || 16
    // Sizes from the DMS theme (Theme.fontSizeMedium, Theme.spacingS, ...).
    readonly property real fontSize: theme.fontSize || 14
    readonly property real glyphSize: theme.iconSize || 20
    readonly property real faviconSize: theme.iconSizeSmall || 16
    readonly property real spacingXS: theme.spacingXS || 4
    readonly property real spacingS: theme.spacingS || 8
    readonly property real spacingM: theme.spacingM || 12
    readonly property real buttonSize: glyphSize + 2 * spacingXS
    readonly property real gap: spacingS // from the screen edges

    title: appSpec.name || "Web Popups"
    color: "transparent"
    visible: false

    LayerShell.Window.scope: "webpopups"
    LayerShell.Window.anchors: anchorBits
    // The "top" layer is above every normal window, so a file dialog would
    // open behind the popup: drop below windows while one is open.
    LayerShell.Window.layer: dialogOpen ? LayerShell.Window.LayerBottom : LayerShell.Window.LayerTop
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityOnDemand
    // Margins count from the output edge, like the bar's coordinates.
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.activateOnShow: true
    LayerShell.Window.screenConfiguration: LayerShell.Window.ScreenFromQWindow

    onAppSpecChanged: {
        currentSize = appSpec.size || "medium";
        muted = !!appSpec.muted;
        syncPermissions();
    }

    // ---- placement --------------------------------------------------------

    function clamp(v, lo, hi) {
        return Math.max(lo, Math.min(v, hi));
    }

    function place(p) {
        if (!p || !p.edge)
            return;
        lastPlace = p;
        const screens = Qt.application.screens;
        let scr = screens.length ? screens[0] : null;
        for (let i = 0; i < screens.length; i++) {
            if (screens[i].name === p.screen)
                scr = screens[i];
        }
        if (!scr)
            return;
        win.screen = scr;
        screenName = scr.name;

        const vertical = p.edge === "left" || p.edge === "right";
        const offset = p.offset || 0;
        const along = p.along || 0;
        const spec = sizes[currentSize] || sizes.medium;
        let w = scr.width * spec[0];
        let h = scr.height * spec[1];
        // Keep the popup on screen: the bar side loses `offset`.
        w = Math.round(Math.min(w, scr.width - (vertical ? offset + gap : 2 * gap)));
        h = Math.round(Math.min(h, scr.height - (vertical ? 2 * gap : offset + gap)));
        win.width = w;
        win.height = h;

        const A = LayerShell.Window;
        let m = [0, 0, 0, 0]; // left, top, right, bottom
        if (vertical) {
            anchorBits = (p.edge === "left" ? A.AnchorLeft : A.AnchorRight) | A.AnchorTop;
            m[p.edge === "left" ? 0 : 2] = offset;
            m[1] = Math.round(clamp(along - h / 2, gap, scr.height - h - gap));
        } else {
            anchorBits = (p.edge === "top" ? A.AnchorTop : A.AnchorBottom) | A.AnchorLeft;
            m[p.edge === "top" ? 1 : 3] = offset;
            m[0] = Math.round(clamp(along - w / 2, gap, scr.width - w - gap));
        }
        backend.setMargins(win, m[0], m[1], m[2], m[3]);
    }

    function cycleSize() {
        outputMenu.visible = false;
        const i = sizeOrder.indexOf(currentSize);
        currentSize = sizeOrder[(i + 1) % sizeOrder.length];
        backend.valueChosen(appId, "size", currentSize);
        if (lastPlace && win.visible)
            remap(lastPlace);
    }

    function toggleMuted() {
        muted = !muted;
        backend.valueChosen(appId, "muted", muted);
    }

    // The helper moves the app's sound right away; the page keeps playing.
    function chooseOutput(name) {
        outputMenu.visible = false;
        if (name !== audioOutput)
            backend.valueChosen(appId, "audioOutput", name);
    }

    // A layer surface takes its output, size and margins when it is mapped,
    // so a visible popup is unmapped and mapped again to apply new ones.
    // The old surface's late focus loss must not count as a click outside,
    // so autohide waits until the new one is activated.
    // The new surface is mapped after a short pause: mapped at once, the
    // compositor hands focus back to the previous window after the new
    // surface was activated, and autohide closes it.
    function remap(p) {
        outputMenu.visible = false;
        wasActive = false;
        win.visible = false;
        remapTimer.pending = p;
        remapTimer.restart();
    }

    Timer {
        id: remapTimer
        property var pending: null
        interval: 50
        onTriggered: win.reveal(pending)
    }

    function reveal(p) {
        place(p);
        win.visible = true;
        win.requestActivate();
        view.forceActiveFocus();
    }

    // ---- showing and hiding -------------------------------------------------

    // Shown or about to be shown again by remap().
    readonly property bool shown: visible || remapTimer.running

    function showAt(p) {
        if (shown) {
            remap(p); // e.g. moved to the bar on another monitor
        } else {
            wasActive = false;
            reveal(p);
        }
    }

    function hidePopup() {
        remapTimer.stop();
        outputMenu.visible = false;
        if (dialogOpen)
            fileDialog.reject(); // answer the page before a teardown
        win.visible = false;
        backend.popupHidden(appId);
    }

    function toggle(p) {
        const target = p && p.screen ? p.screen : "";
        if (shown) {
            if (target && screenName && target !== screenName)
                showAt(p); // clicked on another monitor: move there
            else if (win.active || !pinned)
                hidePopup();
            else
                win.requestActivate();
        } else if (!backend.recentlyAutohidden(appId, target)) {
            showAt(p);
        }
    }

    onVisibleChanged: {
        if (!visible)
            return;
        // Chromium drops the frame of a page hidden for a few minutes and
        // does not draw a new one when the window is shown again, leaving
        // stale GPU memory on screen. Hiding and showing the view makes it
        // render again.
        view.visible = false;
        view.visible = true;
        faviconImage.saveFavicon();
    }

    onActiveChanged: {
        if (active) {
            wasActive = true;
        } else if (wasActive && !pinned && !dialogOpen && visible) {
            backend.markAutohidden(appId, screenName);
            hidePopup();
        }
    }

    function reloadPage() {
        view.reload();
    }

    function goHome() {
        initialLoad = true;
        view.url = appSpec.url;
    }

    // ---- permissions --------------------------------------------------------

    readonly property var mediaTypes: [WebEnginePermission.PermissionType.MediaAudioCapture,
        WebEnginePermission.PermissionType.MediaVideoCapture,
        WebEnginePermission.PermissionType.MediaAudioVideoCapture]

    function permitted(type) {
        const T = WebEnginePermission.PermissionType;
        const mic = !!appSpec.allowMicrophone;
        const cam = !!appSpec.allowCamera;
        switch (type) {
        case T.Notifications:
            return true;
        case T.MediaAudioCapture:
            return mic;
        case T.MediaVideoCapture:
            return cam;
        case T.MediaAudioVideoCapture:
            return mic && cam;
        default:
            return false; // screen sharing, location, clipboard, ...
        }
    }

    function allowed(type, origin) {
        return backend.isAppOrigin(appId, origin) && permitted(type);
    }

    // Pre-grant notifications to the app's origins so the page sees
    // "granted" on load, and revoke media access switched off in settings.
    // Access switched back on is reset, so the next request asks again.
    function syncPermissions() {
        if (!profile)
            return; // appSpec is set before profile on creation
        const origins = backend.appOrigins(appId);
        for (let i = 0; i < origins.length; i++) {
            const o = origins[i];
            profile.queryPermission(o, WebEnginePermission.PermissionType.Notifications).grant();
            for (let j = 0; j < mediaTypes.length; j++) {
                const permission = profile.queryPermission(o, mediaTypes[j]);
                if (!permitted(mediaTypes[j]))
                    permission.deny();
                else if (permission.state === WebEnginePermission.State.Denied)
                    permission.reset();
            }
        }
    }

    // ---- web engine ---------------------------------------------------------

    // Notifications are connected in the helper, on the profile. Downloads
    // are handed over to it too, so they finish (and notify) even when this
    // popup is dropped first.
    Connections {
        target: win.profile

        function onDownloadRequested(download) {
            backend.startDownload(win.appId, download);
        }
    }

    FileDialog {
        id: fileDialog
        property var request: null
        parentWindow: null // a layer surface cannot parent a dialog
        onAccepted: finish(selectedFiles)
        onRejected: finish([])

        function finish(urls) {
            const req = request;
            request = null;
            win.dialogOpen = false;
            if (req) {
                const paths = [];
                for (let i = 0; i < urls.length; i++)
                    paths.push(decodeURIComponent(urls[i].toString().replace(/^file:\/\//, "")));
                if (paths.length)
                    req.dialogAccept(paths);
                else
                    req.dialogReject();
            }
            if (win.visible)
                win.requestActivate();
        }
    }

    Rectangle {
        id: frame
        anchors.fill: parent
        color: win.bgColor
        radius: win.radius
        border.width: 1
        border.color: win.borderColor
        clip: true

        // The icon font of the running shell (sent by the plugin), else the
        // one found by the helper, else an installed "Material Symbols Rounded".
        FontLoader {
            id: iconFont
            readonly property string path: win.theme.iconFont || backend?.iconFontPath || ""
            source: path ? "file://" + path : ""
        }

        Item {
            id: header
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: win.buttonSize + win.spacingM

            Image {
                id: faviconImage
                anchors.left: parent.left
                anchors.leftMargin: win.spacingM
                anchors.verticalCenter: parent.verticalCenter
                width: win.faviconSize
                height: win.faviconSize
                sourceSize.width: 64
                sourceSize.height: 64
                source: view.icon
                visible: status === Image.Ready
                onStatusChanged: saveFavicon()
                function saveFavicon() {
                    if (status !== Image.Ready || !win.visible)
                        return;
                    grabToImage(result => {
                        if (result.saveToFile(backend.faviconPath(win.appId)))
                            backend.faviconSaved(win.appId);
                    }, Qt.size(64, 64));
                }
            }

            Text {
                anchors.left: faviconImage.visible ? faviconImage.right : parent.left
                anchors.leftMargin: faviconImage.visible ? win.spacingS : win.spacingM
                anchors.right: buttons.left
                anchors.rightMargin: win.spacingS
                anchors.verticalCenter: parent.verticalCenter
                text: win.appSpec.name || ""
                color: win.fgColor
                font.family: win.theme.font || ""
                font.pixelSize: win.fontSize
                font.weight: Font.DemiBold
                elide: Text.ElideRight
            }

            Row {
                id: buttons
                anchors.right: parent.right
                anchors.rightMargin: win.spacingS
                anchors.verticalCenter: parent.verticalCenter
                spacing: 2

                HeaderButton { glyph: "arrow_back"; tip: "Back"; onClicked: view.goBack() }
                HeaderButton { glyph: "refresh"; tip: "Reload"; onClicked: view.reload() }
                HeaderButton {
                    glyph: win.currentSize === "large" ? "close_fullscreen" : "open_in_full"
                    tip: "Size: " + win.currentSize
                    onClicked: win.cycleSize()
                }
                HeaderButton {
                    glyph: win.muted ? "volume_off" : "volume_up"
                    tip: win.muted ? "Unmute" : "Mute"
                    active: win.muted
                    onClicked: win.toggleMuted()
                }
                HeaderButton {
                    id: outputButton
                    visible: win.outputs.length > 1 || win.audioOutput !== ""
                    glyph: "speaker"
                    tip: "Audio output"
                    active: win.audioOutput !== "" || outputMenu.visible
                    onClicked: outputMenu.visible = !outputMenu.visible
                }
                HeaderButton {
                    glyph: "keep"; tip: "Keep open"
                    active: win.pinned
                    onClicked: win.pinned = !win.pinned
                }
                HeaderButton {
                    glyph: "settings"; tip: "Web Popups settings"
                    onClicked: {
                        win.hidePopup();
                        backend.openSettings();
                    }
                }
                HeaderButton { glyph: "close"; tip: "Close"; onClicked: win.hidePopup() }
            }
        }

        WebEngineView {
            id: view
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: header.bottom
            anchors.bottom: parent.bottom
            anchors.margins: 1
            profile: win.profile
            settings.screenCaptureEnabled: false
            settings.localContentCanAccessFileUrls: false
            settings.localContentCanAccessRemoteUrls: false
            settings.javascriptCanOpenWindows: false
            settings.webRTCPublicInterfacesOnly: true // no local IPs to sites
            settings.navigateOnDropEnabled: false
            settings.fullScreenSupportEnabled: true
            audioMuted: win.muted

            onRecentlyAudibleChanged: backend.audibleChanged(win.appId, recentlyAudible)

            // Fullscreen (e.g. YouTube videos) fills the popup.
            onFullScreenRequested: request => request.accept()

            onTitleChanged: backend.titleChanged(win.appId, title)

            onLoadingChanged: info => {
                if (info.status === WebEngineLoadingInfo.LoadSucceededStatus && win.initialLoad) {
                    win.initialLoad = false;
                    // Where the configured URL lands belongs to the app too.
                    backend.addAppOrigin(win.appId, view.url.toString());
                    win.syncPermissions();
                }
            }

            // New windows (target=_blank, window.open) go to the browser,
            // but only when the user clicked: pages cannot open tabs alone.
            onNewWindowRequested: request => {
                if (request.userInitiated)
                    backend.openExternal(request.requestedUrl.toString());
            }

            // Links to other schemes (mailto:, custom handlers) never run
            // here. A link the user clicked in the page itself goes to the
            // system, and only mailto/http(s); redirects and frames get nothing.
            onNavigationRequested: request => {
                const url = request.url.toString();
                if (!/^(https?|about|data|blob|chrome-error):/i.test(url)) {
                    request.reject();
                    if (request.isMainFrame
                            && request.navigationType === WebEngineNavigationRequest.LinkClickedNavigation)
                        backend.openExternal(url);
                }
            }

            onPermissionRequested: permission => {
                if (win.allowed(permission.permissionType, permission.origin.toString()))
                    permission.grant();
                else
                    permission.deny();
            }

            onFileDialogRequested: request => {
                request.accepted = true;
                fileDialog.request = request;
                const multiple = request.mode === FileDialogRequest.FileModeOpenMultiple;
                fileDialog.fileMode = multiple ? FileDialog.OpenFiles : FileDialog.OpenFile;
                win.dialogOpen = true;
                fileDialog.open();
            }
        }
    }

    // Audio output menu, below the header at the right.
    FontMetrics {
        id: menuMetrics
        font.family: win.theme.font || ""
        font.pixelSize: win.fontSize
    }

    // A click in the page (or anywhere outside the menu) closes the menu.
    MouseArea {
        anchors.fill: parent
        z: 9
        visible: outputMenu.visible
        onPressed: outputMenu.visible = false
    }

    Rectangle {
        id: outputMenu
        visible: false
        z: 10
        anchors.right: parent.right
        anchors.rightMargin: win.spacingS
        y: header.height
        readonly property real labelWidth: Math.max(...entries.map(e => menuMetrics.advanceWidth(e.label)))
        width: Math.min(parent.width - 2 * win.spacingS,
                        labelWidth + win.glyphSize + 3 * win.spacingS + 2 * win.spacingXS + 2)
        height: menuColumn.implicitHeight + 2 * win.spacingXS
        radius: win.spacingS
        color: win.bgColor
        border.width: 1
        border.color: win.borderColor

        // The chosen output may be unplugged (or hidden in DMS): still list it.
        readonly property var entries: {
            const list = [{ name: "", label: "Default output" }].concat(win.outputs);
            if (win.audioOutput && !win.outputs.some(o => o.name === win.audioOutput)) {
                const known = (win.theme.outputLabels || {})[win.audioOutput];
                list.push({ name: win.audioOutput, label: known || win.audioOutput + " (not connected)" });
            }
            return list;
        }

        Column {
            id: menuColumn
            anchors.fill: parent
            anchors.margins: win.spacingXS

            Repeater {
                model: outputMenu.entries

                Rectangle {
                    id: entry
                    required property var modelData
                    readonly property bool selected: modelData.name === win.audioOutput
                    width: menuColumn.width
                    height: win.buttonSize
                    radius: win.spacingXS
                    color: entryMouse.containsMouse ? Qt.alpha(win.fgColor, 0.10) : "transparent"

                    Text {
                        id: checkGlyph
                        anchors.left: parent.left
                        anchors.leftMargin: win.spacingS
                        anchors.verticalCenter: parent.verticalCenter
                        width: win.glyphSize
                        text: entry.selected ? "check" : ""
                        font.family: win.iconFamily
                        font.pixelSize: win.glyphSize
                        color: win.accentColor
                    }

                    Text {
                        anchors.left: checkGlyph.right
                        anchors.leftMargin: win.spacingS
                        anchors.right: parent.right
                        anchors.rightMargin: win.spacingS
                        anchors.verticalCenter: parent.verticalCenter
                        text: entry.modelData.label
                        textFormat: Text.PlainText // device names come from the devices
                        color: entry.selected ? win.accentColor : win.fgColor
                        font.family: win.theme.font || ""
                        font.pixelSize: win.fontSize
                        elide: Text.ElideRight
                    }

                    MouseArea {
                        id: entryMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: win.chooseOutput(entry.modelData.name)
                    }
                }
            }
        }
    }

    // ---- header button tooltips ---------------------------------------------

    property Item tipButton: null

    function showTip(button) {
        tipButton = button;
        tipTimer.restart();
    }

    function hideTip(button) {
        if (tipButton !== button)
            return;
        tipTimer.stop();
        tipButton = null;
        tipBox.visible = false;
    }

    Timer {
        id: tipTimer
        interval: 600
        onTriggered: {
            const b = win.tipButton;
            if (!b || outputMenu.visible)
                return;
            tipText.text = b.tip;
            const pos = b.mapToItem(win.contentItem, 0, 0);
            tipBox.x = Math.round(win.clamp(pos.x + b.width / 2 - tipBox.width / 2,
                                            win.spacingXS, win.width - tipBox.width - win.spacingXS));
            tipBox.y = Math.round(pos.y + b.height + win.spacingXS);
            tipBox.visible = true;
        }
    }

    Rectangle {
        id: tipBox
        visible: false
        z: 20
        width: tipText.implicitWidth + 2 * win.spacingS
        height: tipText.implicitHeight + win.spacingXS * 2
        radius: win.spacingXS
        color: win.fgColor

        Text {
            id: tipText
            anchors.centerIn: parent
            textFormat: Text.PlainText
            color: win.bgColor
            font.family: win.theme.font || ""
            font.pixelSize: win.fontSize * 0.9
        }
    }

    readonly property string iconFamily: iconFont.status === FontLoader.Ready ? iconFont.font.family : "Material Symbols Rounded"

    component HeaderButton: Rectangle {
        id: hb
        property string glyph: ""
        property string tip: ""
        property bool active: false
        signal clicked

        width: win.buttonSize
        height: win.buttonSize
        radius: width / 2
        color: active ? Qt.alpha(win.accentColor, 0.18) : (mouse.containsMouse ? Qt.alpha(win.fgColor, 0.10) : "transparent")

        Text {
            anchors.centerIn: parent
            text: hb.glyph
            font.family: win.iconFamily
            font.pixelSize: win.glyphSize
            color: hb.active ? win.accentColor : win.fgColor
        }

        MouseArea {
            id: mouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onContainsMouseChanged: containsMouse ? win.showTip(hb) : win.hideTip(hb)
            onClicked: {
                win.hideTip(hb);
                hb.clicked();
            }
        }
    }

    Component.onCompleted: {
        syncPermissions();
        view.url = appSpec.url;
    }
}
