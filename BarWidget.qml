import QtQuick
import QtQuick.Effects
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

BarWidget {
  id: root
  moduleName: "dailen.omartfy"

  readonly property var ntfyService: bar && bar.shell
    ? bar.shell.serviceFor("dailen.omartfy") : null
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true : false
  readonly property int unreadCount: ntfyService ? ntfyService.unreadCount : 0
  readonly property int urgentUnreadCount: ntfyService ? ntfyService.urgentUnreadCount : 0
  property bool dndPopupOpen: false
  property double nowMs: Date.now()
  readonly property bool dndActive: ntfyService ? ntfyService.dndActive : false
  readonly property double muteUntil: ntfyService ? ntfyService.muteUntil : 0
  readonly property var servers: ntfyService ? ntfyService.servers : []
  readonly property int enabledCount: servers.filter(function(server) { return server.enabled !== false }).length
  readonly property int connectedCount: servers.filter(function(server) {
    return server.enabled !== false && server.state === "connected"
  }).length
  readonly property color barTextColor: bar
    ? (bar.barForeground !== undefined ? bar.barForeground : bar.foreground)
    : Color.foreground
  readonly property color iconColor: dndActive
    ? Util.alpha(barTextColor, 0.55)
    : (urgentUnreadCount > 0 ? Color.urgent : barTextColor)
  readonly property string badgeText: unreadCount > 99 ? "99+" : String(unreadCount)

  function open() {
    root.dndPopupOpen = false
    if (panelLoader.item) panelLoader.item.open()
  }
  function close() {
    root.dndPopupOpen = false
    if (panelLoader.item) panelLoader.item.close()
  }
  function toggle() {
    root.dndPopupOpen = false
    if (panelLoader.item) panelLoader.item.toggle()
  }
  function closeForPopoutSwitch() {
    root.dndPopupOpen = false
    if (panelLoader.item && typeof panelLoader.item.closeForPopoutSwitch === "function")
      panelLoader.item.closeForPopoutSwitch()
    else root.close()
  }

  function dndStatus() {
    if (!root.dndActive) return "DND off"
    if (root.muteUntil < 0) return "DND until turned off"
    var minutes = Math.max(1, Math.ceil((root.muteUntil * 1000 - root.nowMs) / 60000))
    if (minutes < 60) return "DND " + minutes + "m remaining"
    return "DND " + Math.ceil(minutes / 60) + "h remaining"
  }

  function setMute(durationSeconds) {
    if (root.ntfyService) root.ntfyService.setMute(durationSeconds)
    root.dndPopupOpen = false
  }

  function untilTomorrowSeconds() {
    var now = new Date()
    var target = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0, 0, 0)
    return Math.max(3600, Math.ceil((target.getTime() - now.getTime()) / 1000))
  }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
    panelLoader.item.ntfyService = root.ntfyService
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onNtfyServiceChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    tooltipText: root.dndActive
      ? root.dndStatus()
      : (root.servers.length === 0
        ? "Add a server to begin"
        : Model.serverSummary(root.servers, root.unreadCount))
    iconComponent: Component {
      Item {
        Image {
          id: mark
          anchors.fill: parent
          anchors.margins: Style.space(2)
          source: Qt.resolvedUrl("assets/ntfy-mask.svg")
          fillMode: Image.PreserveAspectFit
          sourceSize.width: Math.round(width * Screen.devicePixelRatio)
          sourceSize.height: Math.round(height * Screen.devicePixelRatio)
          visible: false
          layer.enabled: true
        }

        MultiEffect {
          anchors.fill: mark
          source: mark
          visible: mark.status === Image.Ready
          // The upstream mask is black. Lift opaque pixels to white before
          // tinting so symbolic colorization tracks the bar foreground.
          brightness: 1.0
          colorization: 1.0
          colorizationColor: root.iconColor
        }

        Text {
          anchors.centerIn: parent
          visible: mark.status === Image.Error
          text: "󰂚"
          textFormat: Text.PlainText
          color: root.iconColor
          font.family: button.fontFamily
          font.pixelSize: button.fontSize
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
      else if (buttonCode === Qt.MiddleButton && root.ntfyService) root.ntfyService.reload()
      else if (buttonCode === Qt.RightButton) {
        var nextOpen = !root.dndPopupOpen
        if (panelLoader.item) panelLoader.item.close()
        root.dndPopupOpen = nextOpen
      }
    }
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.dndActive
    onTriggered: root.nowMs = Date.now()
  }

  PopupCard {
    id: dndPopup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.dndPopupOpen
    contentWidth: dndPopup.fittedContentWidth(Style.space(220))
    contentHeight: dndPopup.fittedContentHeight(dndColumn.implicitHeight)

    Column {
      id: dndColumn
      anchors.fill: parent
      spacing: Style.space(6)

      Text {
        text: root.dndStatus()
        textFormat: Text.PlainText
        color: Color.foreground
        font.family: button.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }

      Button {
        width: parent.width
        text: "Mute for 1 hour"
        onClicked: root.setMute(60 * 60)
      }
      Button {
        width: parent.width
        text: "Mute for 4 hours"
        onClicked: root.setMute(4 * 60 * 60)
      }
      Button {
        width: parent.width
        text: "Mute for 8 hours"
        onClicked: root.setMute(8 * 60 * 60)
      }
      Button {
        width: parent.width
        text: "Mute until tomorrow"
        onClicked: root.setMute(root.untilTomorrowSeconds())
      }
      Button {
        width: parent.width
        text: "Mute until turned off"
        onClicked: root.setMute(-1)
      }
      Button {
        width: parent.width
        visible: root.dndActive
        text: "Turn DND off"
        onClicked: root.setMute(0)
      }
    }
  }

  Rectangle {
    visible: root.unreadCount > 0 && !root.dndActive
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.rightMargin: Style.space(1)
    anchors.topMargin: Style.space(1)
    radius: height / 2
    color: root.urgentUnreadCount > 0 ? Color.urgent : Color.accent
    implicitWidth: Math.max(implicitHeight, badgeLabel.implicitWidth + Style.space(4))
    implicitHeight: Style.space(12)

    Text {
      id: badgeLabel
      anchors.centerIn: parent
      text: root.badgeText
      textFormat: Text.PlainText
      color: Color.background
      font.family: button.fontFamily
      font.pixelSize: Style.space(8)
      font.bold: true
    }
  }

  Text {
    visible: root.dndActive
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    text: "z"
    textFormat: Text.PlainText
    color: root.barTextColor
    font.family: button.fontFamily
    font.pixelSize: Style.space(8)
    font.bold: true
  }

  Rectangle {
    visible: root.enabledCount > 0 && root.connectedCount < root.enabledCount
    anchors.left: parent.left
    anchors.bottom: parent.bottom
    width: Style.space(5)
    height: width
    radius: width / 2
    color: root.connectedCount > 0 ? "#d89b35" : Color.urgent
  }
}
