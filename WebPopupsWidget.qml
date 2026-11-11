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
    // Helper state for every app (unread, favicon, sound), from state.json.
    property var appState: ({})
    readonly property int unread: appState[variantId]?.unread ?? 0
    readonly property string favicon: appState[variantId]?.favicon ?? ""
    readonly property string sound: soundOf(variantId)

    // "muted", "audible" (played sound recently) or "" for a loaded app.
    function soundOf(id) {
        const entry = appState[id];
        return entry?.muted ? "muted" : (entry?.audible ? "audible" : "");
    }

    // ---- grouping ----------------------------------------------------------
    // With "Group the app buttons" on, the first Web Popups button of a bar
    // shows every app of that bar and expands on hover; the others hide.
    // Hidden buttons stay loaded, so they keep registering their app.

    readonly property bool grouped: !!pluginData?.groupIcons

    // This bar's Web Popups apps, in bar order.
    readonly property var barAppIds: {
        const ids = [];
        const prefix = pluginId + ":";
        for (const key of ["leftWidgets", "centerWidgets", "rightWidgets"]) {
            for (const w of (barConfig?.[key] ?? [])) {
                const id = typeof w === "string" ? w : w?.id;
                if (id?.startsWith(prefix) && w?.enabled !== false && !ids.includes(id.slice(prefix.length)))
                    ids.push(id.slice(prefix.length));
            }
        }
        return ids;
    }
    readonly property var groupApps: barAppIds.map(id => variants.find(v => v.id === id)).filter(v => !!v?.url)
    readonly property bool isLeader: grouped && barAppIds[0] === variantId
    readonly property bool hiddenByGroup: grouped && !isLeader && barAppIds.includes(variantId)
    property bool groupOpen: false

    onHiddenByGroupChanged: applyGroup()
    Component.onCompleted: applyGroup()

    function applyGroup() {
        if (hiddenByGroup)
            setVisibilityOverride(false);
        else
            clearVisibilityOverride();
    }

    HoverHandler {
        id: groupHover
        enabled: root.isLeader
        onHoveredChanged: {
            if (hovered) {
                closeTimer.stop();
                openTimer.restart();
            } else {
                openTimer.stop();
                closeTimer.restart();
            }
        }
    }

    Timer {
        id: openTimer
        interval: 150
        onTriggered: root.groupOpen = true
    }

    Timer {
        id: closeTimer
        interval: 400
        onTriggered: root.groupOpen = false
    }

    Connections {
        target: PluginService
        function onPluginDataChanged(changedPluginId) {
            if (changedPluginId !== root.pluginId || !root.variantId)
                return;
            const data = PluginService.getPluginVariantData(root.pluginId, root.variantId);
            if (!data)
                return;
            const changed = JSON.stringify(root.specFor(root.variantId, data)) !== JSON.stringify(root.appSpec());
            root.variantData = data;
            // Apply settings changes to a loaded popup right away.
            if (changed && data.url)
                root.send({ cmd: "register", app: root.appSpec() });
        }
    }

    // ---- talking to the helper -------------------------------------------

    // The helper's description of one app (a variant's settings).
    function specFor(id, data) {
        return {
            id: id,
            name: data?.name || "Web app",
            url: data?.url || "",
            size: data?.size || "medium",
            keepAlive: data?.keepAlive ?? true,
            allowMicrophone: data?.allowMicrophone ?? false,
            allowCamera: data?.allowCamera ?? false,
            muted: data?.muted ?? false
        };
    }

    function appSpec() {
        return specFor(variantId, variantData);
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
            font: Theme.fontFamily,
            fontSize: Theme.fontSizeMedium,
            iconSize: Theme.iconSizeMedium,
            iconSizeSmall: Theme.iconSizeSmall,
            spacingXS: Theme.spacingXS,
            spacingS: Theme.spacingS,
            spacingM: Theme.spacingM
        };
    }

    function send(message) {
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
        if (isLeader)
            groupOpen = !groupOpen; // a click beside the icons, or a touch
        else if (variantData?.url)
            send({ cmd: "toggle", app: appSpec(), place: placement(x, y, width, screen) });
    }

    // Open one app of the group next to its own icon.
    function toggleFromGroup(app, icon) {
        const size = isVertical ? icon.height : icon.width;
        const anchor = surfaceContext?.popupAnchor(root, section, icon, size);
        if (!anchor)
            return;
        const t = anchor.trigger;
        // popupAnchor gives the icon's center on vertical bars, its start
        // on horizontal ones; placement() expects the start.
        const start = isVertical ? t.y - size / 2 : t.y;
        const data = PluginService.getPluginVariantData(pluginId, app.id) || app; // `variants` can be stale
        send({ cmd: "toggle", app: specFor(app.id, data), place: placement(t.x, start, size, anchor.screen) });
    }

    // Web apps open on click only, even when the bar opens popouts on hover.
    function triggerHoverPopout(widgetId) {
    }

    // Stop the helper, and every page it holds, when the plugin is disabled
    // or reloaded: it runs in its own session and would outlive the plugin.
    // DMS destroys the buttons before it emits pluginUnloaded, so check here
    // whether the plugin itself is gone (not just this button, e.g. moved).
    Component.onDestruction: {
        if (pluginId && !PluginService.pluginWidgetComponents[pluginId])
            send({ cmd: "quit" });
    }

    pillRightClickAction: () => {
        if (!isLeader && variantData?.url)
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
                root.appState = JSON.parse(text()) || {};
                const entry = root.appState[root.variantId];
                // Settings changed from the popup header (size, mute): save
                // them. Then tell the helper, which stops reporting them.
                const chosen = entry?.chosen;
                if (chosen) {
                    if (Object.keys(chosen).some(key => chosen[key] !== root.variantData?.[key]))
                        PluginService.updatePluginVariant(root.pluginId, root.variantId, chosen);
                    root.send({ cmd: "register", app: Object.assign(root.appSpec(), chosen) });
                }
            } catch (e) {
                root.appState = {};
            }
        }
    }

    // ---- bar pill ----------------------------------------------------------

    // An app icon (favicon, or the Material Symbols icon until the page has
    // loaded) with its unread badge and sound mark.
    component AppGlyph: Item {
        id: glyph
        property string favicon: root.favicon
        property string icon: root.appIcon
        property int unread: root.unread
        property string sound: root.sound

        implicitWidth: root.iconSize
        implicitHeight: root.iconSize

        Image {
            id: faviconImage
            anchors.fill: parent
            source: glyph.favicon ? "file://" + glyph.favicon : ""
            sourceSize.width: root.iconSize * 2
            sourceSize.height: root.iconSize * 2
            fillMode: Image.PreserveAspectFit
            smooth: true
            cache: false
            visible: status === Image.Ready
        }

        DIcon {
            anchors.centerIn: parent
            name: glyph.icon
            size: root.iconSize
            color: Theme.surfaceText
            visible: faviconImage.status !== Image.Ready
        }

        Rectangle {
            visible: glyph.unread > 0
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.rightMargin: -width / 3
            anchors.topMargin: -height / 3
            height: Math.max(Theme.fontSizeSmall, root.iconSize * 0.55)
            width: Math.max(height, badgeText.implicitWidth + Theme.spacingS)
            radius: height / 2
            color: Theme.primary

            StyledText {
                id: badgeText
                anchors.centerIn: parent
                text: glyph.unread > 99 ? "99+" : String(glyph.unread)
                font.pixelSize: parent.height * 0.75
                font.weight: Font.Bold
                color: Theme.onPrimary
            }
        }

        Rectangle {
            visible: glyph.sound !== ""
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.rightMargin: -width / 3
            anchors.bottomMargin: -height / 3
            width: Math.round(root.iconSize * 0.55)
            height: width
            radius: width / 2
            color: Theme.surfaceContainer

            DIcon {
                anchors.centerIn: parent
                name: glyph.sound === "muted" ? "volume_off" : "volume_up"
                size: parent.width * 0.8
                color: glyph.sound === "muted" ? Theme.surfaceVariantText : Theme.primary
            }
        }
    }

    // The group: one icon with the total unread count; on hover it grows
    // into a row (or column) with every app.
    component AppGroup: Item {
        id: group
        readonly property bool vertical: root.isVertical
        readonly property real fullLength: root.groupApps.length * root.iconSize
            + Math.max(0, root.groupApps.length - 1) * Theme.spacingS
        readonly property real length: root.groupOpen ? Math.max(root.iconSize, fullLength) : root.iconSize

        implicitWidth: vertical ? root.iconSize : length
        implicitHeight: vertical ? length : root.iconSize
        clip: true

        Behavior on implicitWidth {
            enabled: !SettingsData.reduceMotion
            NumberAnimation { duration: Theme.shortDuration; easing.type: Theme.standardEasing }
        }
        Behavior on implicitHeight {
            enabled: !SettingsData.reduceMotion
            NumberAnimation { duration: Theme.shortDuration; easing.type: Theme.standardEasing }
        }

        AppGlyph {
            anchors.centerIn: parent
            favicon: ""
            icon: "language"
            unread: root.groupApps.reduce((sum, app) => sum + (root.appState[app.id]?.unread ?? 0), 0)
            sound: {
                const sounds = root.groupApps.map(app => root.soundOf(app.id));
                return sounds.includes("audible") ? "audible" : (sounds.includes("muted") ? "muted" : "");
            }
            opacity: root.groupOpen ? 0 : 1
            visible: opacity > 0
            Behavior on opacity { NumberAnimation { duration: Theme.shortDuration } }
        }

        Grid {
            columns: group.vertical ? 1 : Math.max(1, root.groupApps.length)
            spacing: Theme.spacingS
            opacity: root.groupOpen ? 1 : 0
            visible: opacity > 0
            Behavior on opacity { NumberAnimation { duration: Theme.shortDuration } }

            Repeater {
                model: root.groupApps

                AppGlyph {
                    id: member
                    required property var modelData
                    favicon: root.appState[modelData.id]?.favicon ?? ""
                    icon: modelData.icon || "web"
                    unread: root.appState[modelData.id]?.unread ?? 0
                    sound: root.soundOf(modelData.id)

                    MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                        cursorShape: Qt.PointingHandCursor
                        onClicked: mouse => {
                            if (mouse.button === Qt.RightButton)
                                root.send({ cmd: "reload", id: member.modelData.id });
                            else
                                root.toggleFromGroup(member.modelData, member);
                        }
                    }
                }
            }
        }
    }

    horizontalBarPill: Component {
        Loader { sourceComponent: root.isLeader ? groupComponent : glyphComponent }
    }

    verticalBarPill: Component {
        Loader { sourceComponent: root.isLeader ? groupComponent : glyphComponent }
    }

    Component { id: glyphComponent; AppGlyph {} }
    Component { id: groupComponent; AppGroup {} }
}
