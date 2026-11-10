import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services
import qs.DCommon.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root

    property string variantId: ""
    property var variantData: null

    readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("helper/webpopups.py").toString().replace(/^file:\/\//, ""))
    readonly property string statePath: Quickshell.env("XDG_RUNTIME_DIR") + "/webpopups/state.json"

    readonly property string appName: variantData?.name || "Web app"
    readonly property string appIcon: variantData?.icon || "web"
    property int unread: 0
    property string favicon: ""

    Connections {
        target: PluginService
        function onPluginDataChanged(changedPluginId) {
            if (changedPluginId !== root.pluginId || !root.variantId)
                return;
            const data = PluginService.getPluginVariantData(root.pluginId, root.variantId);
            if (data)
                root.variantData = data;
        }
    }

    // ---- talking to the helper -------------------------------------------

    function appSpec() {
        return {
            id: variantId,
            name: appName,
            url: variantData?.url || "",
            size: variantData?.size || "medium",
            keepAlive: variantData?.keepAlive ?? true,
            allowMicrophone: variantData?.allowMicrophone ?? false,
            allowCamera: variantData?.allowCamera ?? false
        };
    }

    // "#aarrggbb", the form QML colors parse.
    function colorString(c) {
        const hex = v => Math.round(v * 255).toString(16).padStart(2, "0");
        return "#" + hex(c.a) + hex(c.r) + hex(c.g) + hex(c.b);
    }

    function themeSpec() {
        return {
            bg: colorString(Theme.surfaceContainer),
            fg: colorString(Theme.surfaceText),
            accent: colorString(Theme.primary),
            border: colorString(Theme.outlineVariant),
            radius: Theme.cornerRadius,
            font: Theme.fontFamily
        };
    }

    function send(message) {
        if (!variantId || !variantData?.url)
            return;
        message.theme = themeSpec();
        Quickshell.execDetached(["python3", helperPath, "send", JSON.stringify(message)]);
    }

    // Bar position: 0 top, 1 bottom, 2 left, 3 right.
    function placement(x, y, width, screen) {
        const position = barConfig?.position ?? 0;
        const edges = ["top", "bottom", "left", "right"];
        const offsets = [y, (screen?.height ?? 0) - y, x, (screen?.width ?? 0) - x];
        const vertical = position === 2 || position === 3;
        return {
            screen: screen?.name ?? "",
            edge: edges[position] ?? "top",
            offset: Math.round(offsets[position] ?? 0),
            along: Math.round(vertical ? y + width / 2 : x + width / 2)
        };
    }

    pillClickAction: (x, y, width, section, screen) => {
        send({ cmd: "toggle", app: appSpec(), place: placement(x, y, width, screen) });
    }

    // Web apps open on click only, even when the bar opens popouts on hover.
    function triggerHoverPopout(widgetId) {
    }

    pillRightClickAction: () => {
        send({ cmd: "reload", id: variantId });
    }

    // Load background apps shortly after login so badges and notifications work
    // before the popup is opened for the first time.
    Timer {
        id: registerTimer
        interval: 3000
        running: !!root.variantId && !!root.variantData?.url
        onTriggered: root.send({ cmd: "register", app: root.appSpec() })
    }

    FileView {
        path: root.statePath
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            try {
                const entry = JSON.parse(text())[root.variantId];
                root.unread = entry?.unread ?? 0;
                root.favicon = entry?.favicon ?? "";
                // Size picked with the popup's own size button: save it.
                // Then tell the helper, which stops reporting it.
                const chosen = entry?.chosenSize;
                if (chosen) {
                    if (chosen !== root.variantData?.size)
                        PluginService.updatePluginVariant(root.pluginId, root.variantId, { size: chosen });
                    root.send({ cmd: "register", app: Object.assign(root.appSpec(), { size: chosen }) });
                }
            } catch (e) {
                root.unread = 0;
            }
        }
    }

    // ---- bar pill ----------------------------------------------------------

    component AppGlyph: Item {
        implicitWidth: root.iconSize
        implicitHeight: root.iconSize

        Image {
            id: faviconImage
            anchors.fill: parent
            source: root.favicon ? "file://" + root.favicon : ""
            sourceSize.width: root.iconSize * 2
            sourceSize.height: root.iconSize * 2
            fillMode: Image.PreserveAspectFit
            smooth: true
            cache: false
            visible: status === Image.Ready
        }

        DIcon {
            anchors.centerIn: parent
            name: root.appIcon
            size: root.iconSize
            color: Theme.surfaceText
            visible: faviconImage.status !== Image.Ready
        }

        Rectangle {
            visible: root.unread > 0
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.rightMargin: -width / 3
            anchors.topMargin: -height / 3
            height: Math.max(12, root.iconSize * 0.55)
            width: Math.max(height, badgeText.implicitWidth + 6)
            radius: height / 2
            color: Theme.primary

            StyledText {
                id: badgeText
                anchors.centerIn: parent
                text: root.unread > 99 ? "99+" : String(root.unread)
                font.pixelSize: parent.height * 0.75
                font.weight: Font.Bold
                color: Theme.onPrimary
            }
        }
    }

    horizontalBarPill: Component {
        AppGlyph {}
    }

    verticalBarPill: Component {
        AppGlyph {}
    }
}
