import QtQuick
import qs.Common

QtObject {
    function check(done) {
        const probe = "import os, sys\n"
            + "from PyQt6.QtWebEngineQuick import QtWebEngineQuick\n"
            + "from PyQt6.QtCore import QLibraryInfo\n"
            + "qml = QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath)\n"
            + "sys.exit(0 if os.path.exists(os.path.join(qml, 'org/kde/layershell/qmldir')) else 1)\n";
        Proc.runCommand("webPopups.depCheck", ["python3", "-c", probe], (stdout, exitCode) => {
            if (exitCode === 0) {
                done(null);
                return;
            }
            done({
                title: "Web Popups needs PyQt6 WebEngine and layer-shell-qt",
                details: "Install the missing packages, then re-enable the plugin.\n\n"
                    + "Fedora:  sudo dnf install python3-pyqt6-webengine layer-shell-qt\n"
                    + "Arch:    sudo pacman -S python-pyqt6-webengine layer-shell-qt\n"
                    + "Other distros:  install PyQt6 with Qt WebEngine and the layer-shell-qt QML module"
            });
        });
    }
}
