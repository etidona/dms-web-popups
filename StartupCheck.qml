import QtQuick
import qs.Common

QtObject {
    readonly property string installHelp: "Install the missing packages, then re-enable the plugin.\n\n"
        + "Fedora:  sudo dnf install python3-pyqt6-webengine layer-shell-qt xdg-utils pulseaudio-utils\n"
        + "Arch:    sudo pacman -S python-pyqt6-webengine layer-shell-qt xdg-utils libpulse\n"
        + "Other distros:  install PyQt6 with Qt WebEngine, the layer-shell-qt QML module, xdg-utils and pactl"

    function check(done) {
        // Exit codes: 1 = PyQt6 WebEngine or layer-shell-qt missing, 3 = pactl missing.
        const probe = "import os, shutil, sys\n"
            + "try:\n"
            + "    from PyQt6.QtWebEngineQuick import QtWebEngineQuick\n"
            + "    from PyQt6.QtCore import QLibraryInfo\n"
            + "except ImportError:\n"
            + "    sys.exit(1)\n"
            + "qml = QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath)\n"
            + "if not os.path.exists(os.path.join(qml, 'org/kde/layershell/qmldir')):\n"
            + "    sys.exit(1)\n"
            + "sys.exit(0 if shutil.which('pactl') else 3)\n";
        Proc.runCommand("webPopups.depCheck", ["python3", "-c", probe], (stdout, exitCode) => {
            if (exitCode === 0) {
                done(null);
                return;
            }
            done({
                title: exitCode === 3 ? "Web Popups needs pactl for the audio output of each app"
                                      : "Web Popups needs PyQt6 WebEngine and layer-shell-qt",
                details: installHelp
            });
        });
    }
}
