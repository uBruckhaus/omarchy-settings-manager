import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// The bar icon toggles Settings Manager: closes it when it is the window in front, else starts it
// or brings it to the front (a normal app window; the app itself keeps one instance).
// Right-click opens its Updates page.
BarWidget {
  id: root

  moduleName: "ubruckhaus.settings-manager"

  readonly property string launcherPath: Qt.resolvedUrl("settings-manager").toString().replace(/^file:\/\//, "")

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\u{F0431}"
    tooltipText: "Settings Manager"

    onPressed: function(mouseButton) {
      if (!root.bar) return
      if (mouseButton === Qt.RightButton)
        Quickshell.execDetached([root.launcherPath, "--page", "updates"])
      else
        Quickshell.execDetached([root.launcherPath, "--toggle"])
    }
  }
}
