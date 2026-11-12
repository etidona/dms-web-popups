import QtQuick
import Quickshell
import qs.Common
import qs.Services
import qs.DCommon.Widgets
import qs.Modules.Plugins

PluginSettings {
    id: root
    pluginId: "webPopups"

    // Built-in web apps. Icons are Material Symbols names; the site's favicon
    // replaces them on the bar once the page has loaded. Kept inline rather
    // than in a .pragma library, which the QML engine caches across reloads.
    readonly property var presets: [
        { name: "WhatsApp",    url: "https://web.whatsapp.com",    icon: "chat",          size: "medium", keepAlive: true  },
        { name: "Telegram",    url: "https://web.telegram.org/k/", icon: "send",          size: "medium", keepAlive: true  },
        { name: "Discord",     url: "https://discord.com/app",     icon: "forum",         size: "large",  keepAlive: true  },
        { name: "Proton Mail", url: "https://mail.proton.me",      icon: "mail",          size: "large",  keepAlive: true  },
        { name: "Claude",      url: "https://claude.ai",           icon: "smart_toy",     size: "medium", keepAlive: true  },
        { name: "YouTube",     url: "https://m.youtube.com",       icon: "smart_display", size: "small",  keepAlive: false },
        { name: "Syncthing",   url: "http://127.0.0.1:8384",       icon: "sync",          size: "large",  keepAlive: false }
    ]
    readonly property var sizes: ["small", "medium", "large"]
    readonly property var sizeLabels: sizes.map(s => s[0].toUpperCase() + s.slice(1))

    // Audio outputs, from DMS's AudioService: real outputs only (Pipewire
    // also lists app streams as sinks). Same as in WebPopupsWidget.qml.
    function isOutput(node) {
        return !!node && !node.isStream && node.properties?.["media.class"] === "Audio/Sink";
    }

    // [{ name, label }], without the outputs hidden in DMS.
    function outputList() {
        return AudioService.getAvailableSinks().filter(isOutput).map(n => ({ name: n.name, label: AudioService.displayName(n) }));
    }

    // { name: label } for every output, also those hidden in DMS.
    function outputLabels() {
        const map = {};
        for (const node of AudioService.allNodes || []) {
            if (isOutput(node))
                map[node.name] = AudioService.displayName(node);
        }
        return map;
    }

    // For the per-app output choice; "" is the default output.
    readonly property var sinks: outputList()
    readonly property var sinkLabels: outputLabels()

    function outputValues(current) {
        const names = [""].concat(sinks.map(s => s.name));
        if (current && !names.includes(current))
            names.push(current); // chosen, but hidden in DMS or not connected now
        return names;
    }

    function outputLabel(name) {
        if (!name)
            return "Default output";
        return sinkLabels[name] || name + " (not connected)";
    }

    // PluginService mutates its variant list in place, so `variants` never
    // signals a change; keep our own copy and refresh it on every update.
    property var apps: []

    function refreshApps() {
        apps = JSON.parse(JSON.stringify(PluginService.getPluginVariants(pluginId) || []));
    }

    Component.onCompleted: refreshApps()

    Connections {
        target: PluginService
        function onPluginDataChanged(changedPluginId) {
            if (changedPluginId === root.pluginId)
                root.refreshApps();
        }
    }

    readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("helper/webpopups.py").toString().replace(/^file:\/\//, ""))

    function addApp(name, config) {
        const variantId = createVariant(name, config);
        if (!variantId)
            return;
        // Put the new button on the primary bar right away, after the last
        // Web Popups button, so the apps stay together; in the center only
        // when there is none yet.
        const bar = SettingsData.getPrimaryBarConfig();
        if (bar && !insertAfterLastApp(bar, root.pluginId + ":" + variantId))
            SettingsData.addBarWidget(bar.id, "center", root.pluginId + ":" + variantId);
        ToastService.showInfo(name + " added to the bar");
    }

    function insertAfterLastApp(bar, widgetId) {
        const prefix = root.pluginId + ":";
        for (const key of ["rightWidgets", "centerWidgets", "leftWidgets"]) {
            const widgets = bar[key] || [];
            let last = -1;
            widgets.forEach((w, i) => {
                if ((typeof w === "string" ? w : w?.id ?? "").startsWith(prefix))
                    last = i;
            });
            if (last < 0)
                continue;
            const updated = widgets.slice();
            updated.splice(last + 1, 0, { id: widgetId, enabled: true });
            SettingsData.updateBarConfig(bar.id, { [key]: updated });
            return true;
        }
        return false;
    }

    function removeApp(variantId, name) {
        removeVariant(variantId);
        Quickshell.execDetached(["python3", helperPath, "send", JSON.stringify({ cmd: "remove", id: variantId, purge: true })]);
        ToastService.showInfo(name + " removed");
    }

    // Only web addresses: the helper refuses any other scheme.
    function normalizeUrl(text) {
        const url = text.trim();
        if (!url)
            return "";
        if (/^https?:\/\//i.test(url))
            return url;
        // Another scheme ("file://", "ftp://", "javascript:"...): refuse it.
        // "host:port" without a scheme is still a web address.
        if (/^[a-z][a-z0-9+.-]*:\/\//i.test(url) || /^(javascript|data|about|blob|file|mailto):/i.test(url))
            return "";
        return "https://" + url;
    }

    StyledText {
        width: parent.width
        text: "Web Popups"
        font.pixelSize: Theme.fontSizeLarge
        font.weight: Theme.fontWeightMedium
        color: Theme.surfaceText
    }

    StyledText {
        width: parent.width
        text: "Each web app gets its own bar button. Left click opens or closes the popup, right click reloads the page. Logins are kept separately for each app."
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.surfaceVariantText
        wrapMode: Text.WordWrap
    }

    ToggleSetting {
        settingKey: "groupIcons"
        label: "Group the app buttons"
        description: "Show one button on each bar that opens a panel with all your web apps when you hover over it. Right click it, or click the gear in the panel, to open these settings."
        defaultValue: false
    }

    // ---- presets ---------------------------------------------------------

    StyledRect {
        width: parent.width
        height: presetColumn.implicitHeight + Theme.spacingL * 2
        radius: Theme.cornerRadius
        color: Theme.chipSurface

        Column {
            id: presetColumn
            anchors.fill: parent
            anchors.margins: Theme.spacingL
            spacing: Theme.spacingM

            StyledText {
                text: "Add a preset"
                font.pixelSize: Theme.fontSizeMedium
                font.weight: Theme.fontWeightMedium
                color: Theme.surfaceText
            }

            Flow {
                width: parent.width
                spacing: Theme.spacingS

                Repeater {
                    model: root.presets

                    DButton {
                        required property var modelData
                        text: modelData.name
                        iconName: modelData.icon
                        onClicked: root.addApp(modelData.name, {
                            url: modelData.url,
                            icon: modelData.icon,
                            size: modelData.size,
                            keepAlive: modelData.keepAlive,
                            allowMicrophone: false,
                            allowCamera: false,
                            muted: false
                        })
                    }
                }
            }
        }
    }

    // ---- custom app ------------------------------------------------------

    StyledRect {
        width: parent.width
        height: customColumn.implicitHeight + Theme.spacingL * 2
        radius: Theme.cornerRadius
        color: Theme.chipSurface

        Column {
            id: customColumn
            anchors.fill: parent
            anchors.margins: Theme.spacingL
            spacing: Theme.spacingM

            StyledText {
                text: "Add any web app"
                font.pixelSize: Theme.fontSizeMedium
                font.weight: Theme.fontWeightMedium
                color: Theme.surfaceText
            }

            Row {
                width: parent.width
                spacing: Theme.spacingM

                DTextField {
                    id: nameField
                    width: (parent.width - Theme.spacingM * 2) * 0.3
                    placeholderText: "Name"
                }

                DTextField {
                    id: urlField
                    width: (parent.width - Theme.spacingM * 2) * 0.45
                    placeholderText: "https://example.com"
                }

                DTextField {
                    id: iconField
                    width: (parent.width - Theme.spacingM * 2) * 0.25
                    placeholderText: "Icon (web)"
                }
            }

            DButtonGroup {
                id: newSizeGroup
                arrowKeysSelect: false
                model: root.sizeLabels
                currentIndex: 1
                onSelectionChanged: (index, selected) => {
                    if (selected)
                        currentIndex = index;
                }
            }

            DButton {
                text: "Add"
                iconName: "add"
                onClicked: {
                    const url = root.normalizeUrl(urlField.text);
                    if (!nameField.text.trim() || !url) {
                        ToastService.showError("Enter a name and an http or https address");
                        return;
                    }
                    root.addApp(nameField.text.trim(), {
                        url: url,
                        icon: iconField.text.trim() || "web",
                        size: root.sizes[newSizeGroup.currentIndex] || "medium",
                        keepAlive: true,
                        allowMicrophone: false,
                        allowCamera: false,
                        muted: false
                    });
                    nameField.text = "";
                    urlField.text = "";
                    iconField.text = "";
                }
            }
        }
    }

    // ---- configured apps ---------------------------------------------------

    StyledRect {
        width: parent.width
        height: appsColumn.implicitHeight + Theme.spacingL * 2
        radius: Theme.cornerRadius
        color: Theme.chipSurface

        Column {
            id: appsColumn
            anchors.fill: parent
            anchors.margins: Theme.spacingL
            spacing: Theme.spacingM

            StyledText {
                text: "Your web apps"
                font.pixelSize: Theme.fontSizeMedium
                font.weight: Theme.fontWeightMedium
                color: Theme.surfaceText
            }

            StyledText {
                visible: root.apps.length === 0
                text: "No web apps yet. Add a preset or your own above."
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.surfaceVariantText
            }

            Repeater {
                model: root.apps

                StyledRect {
                    id: appCard
                    required property var modelData
                    width: appsColumn.width
                    height: appCardColumn.implicitHeight + Theme.spacingM * 2
                    radius: Theme.cornerRadius
                    color: Theme.chipSurfaceNested

                    Column {
                        id: appCardColumn
                        anchors.fill: parent
                        anchors.margins: Theme.spacingM
                        spacing: Theme.spacingS

                        Row {
                            width: parent.width
                            spacing: Theme.spacingM

                            DIcon {
                                name: appCard.modelData.icon || "web"
                                size: Theme.iconSize
                                color: Theme.surfaceText
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Column {
                                width: parent.width - Theme.iconSize - deleteButton.width - Theme.spacingM * 2
                                anchors.verticalCenter: parent.verticalCenter

                                StyledText {
                                    width: parent.width
                                    text: appCard.modelData.name
                                    font.pixelSize: Theme.fontSizeMedium
                                    color: Theme.surfaceText
                                    elide: Text.ElideRight
                                }

                                StyledText {
                                    width: parent.width
                                    text: appCard.modelData.url
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.surfaceVariantText
                                    elide: Text.ElideRight
                                }
                            }

                            DIconButton {
                                id: deleteButton
                                iconName: "delete"
                                anchors.verticalCenter: parent.verticalCenter
                                onClicked: root.removeApp(appCard.modelData.id, appCard.modelData.name)
                            }
                        }

                        DButtonGroup {
                            arrowKeysSelect: false
                            model: root.sizeLabels
                            currentIndex: Math.max(0, root.sizes.indexOf(appCard.modelData.size || "medium"))
                            onSelectionChanged: (index, selected) => {
                                if (selected)
                                    root.updateVariant(appCard.modelData.id, { size: root.sizes[index] });
                            }
                        }

                        DToggle {
                            width: parent.width
                            text: "Keep loaded in the background"
                            description: "Stays connected when closed, for notifications and unread badges"
                            checked: appCard.modelData.keepAlive ?? true
                            onToggled: checked => root.updateVariant(appCard.modelData.id, { keepAlive: checked })
                        }

                        DToggle {
                            width: parent.width
                            text: "Allow microphone"
                            description: "Needed for calls and voice messages"
                            checked: appCard.modelData.allowMicrophone ?? false
                            onToggled: checked => root.updateVariant(appCard.modelData.id, { allowMicrophone: checked })
                        }

                        DToggle {
                            width: parent.width
                            text: "Allow camera"
                            description: "Needed for video calls"
                            checked: appCard.modelData.allowCamera ?? false
                            onToggled: checked => root.updateVariant(appCard.modelData.id, { allowCamera: checked })
                        }

                        DDropdown {
                            width: parent.width
                            text: "Audio output"
                            description: "Where the app's sound goes; also from the popup header"
                            readonly property var values: root.outputValues(appCard.modelData.audioOutput || "")
                            optionValues: values.map(v => v || "@default")
                            options: values.map(v => root.outputLabel(v))
                            currentValue: appCard.modelData.audioOutput || "@default"
                            onValueChanged: value => {
                                const output = value === "@default" ? "" : value;
                                if (output !== (appCard.modelData.audioOutput || ""))
                                    root.updateVariant(appCard.modelData.id, { audioOutput: output });
                            }
                        }

                        DToggle {
                            width: parent.width
                            text: "Mute audio"
                            description: "Silence the app's sounds; also from the popup header"
                            checked: appCard.modelData.muted ?? false
                            onToggled: checked => root.updateVariant(appCard.modelData.id, { muted: checked })
                        }
                    }
                }
            }
        }
    }
}
