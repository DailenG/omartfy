import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "components"

Panel {
  id: root
  moduleName: "dailen.omartfy"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var ntfyService: null
  readonly property var barIdentity: hostWidget || root
  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family
  property string selectedServerId: "all"
  property int selectedIndex: 0
  property string expandedKey: ""
  property bool searchOpen: false
  property bool editorOpen: false
  property double nowMs: Date.now()
  readonly property string searchQuery: searchOpen ? searchField.text : ""
  readonly property var servers: ntfyService ? ntfyService.servers : []
  readonly property var notifications: ntfyService ? ntfyService.notifications : []
  readonly property var visibleRows: Model.filterRows(notifications, selectedServerId, searchQuery)
  readonly property var tabState: Model.tabLayout(servers, selectedServerId, 3)
  readonly property bool nestedOpen: searchOpen || editorOpen || serverMorePopup.opened
    || clearPopup.opened

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() { root.opened ? root.close() : root.open() }
  function closeForPopoutSwitch() { root.close() }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function selectedRow() {
    if (root.selectedIndex < 0 || root.selectedIndex >= root.visibleRows.length) return null
    return root.visibleRows[root.selectedIndex]
  }

  function serverFor(serverId) {
    for (var index = 0; index < root.servers.length; index++)
      if (root.servers[index].id === serverId) return root.servers[index]
    return null
  }

  function unreadFor(serverId) {
    var count = 0
    for (var index = 0; index < root.notifications.length; index++) {
      var row = root.notifications[index]
      if (row.unread && (serverId === "all" || row.serverId === serverId)) count += 1
    }
    return count
  }

  function selectServer(serverId) {
    root.selectedServerId = String(serverId || "all")
    root.selectedIndex = 0
    root.expandedKey = ""
    serverMorePopup.close()
  }

  function moveCursor(dx, dy) {
    if (dy !== 0 && root.visibleRows.length > 0) {
      root.selectedIndex = Math.max(0, Math.min(root.visibleRows.length - 1,
        root.selectedIndex + dy))
      feed.positionViewAtIndex(root.selectedIndex, ListView.Contain)
    }
    if (dx !== 0) {
      var tabs = root.tabState.tabs
      var index = tabs.findIndex(function(tab) { return tab.id === root.selectedServerId })
      if (index < 0) index = 0
      root.selectServer(tabs[(index + dx + tabs.length) % tabs.length].id)
    }
  }

  // Like tapping a phone notification: a row with a link opens it and counts
  // as read; a row without one expands in place.
  function activateSelected() {
    var row = root.selectedRow()
    if (!row) return
    if (row.click && root.ntfyService && root.ntfyService.helperReady) {
      root.openSpecial("__click")
      root.markSelectedRead()
    } else root.toggleSelected()
  }

  function toggleSelected() {
    var row = root.selectedRow()
    if (!row) return
    root.expandedKey = root.expandedKey === row.notificationKey ? "" : row.notificationKey
  }

  function markSelectedRead() {
    var row = root.selectedRow()
    if (row && row.unread && root.ntfyService)
      root.ntfyService.markNotificationRead(row.notificationKey)
  }

  function deleteSelected() {
    var row = root.selectedRow()
    if (row && root.ntfyService) root.ntfyService.deleteNotification(row.notificationKey)
  }

  function openSpecial(actionId) {
    var row = root.selectedRow()
    if (!row || !root.ntfyService) return
    if (actionId === "__click" && row.click)
      root.ntfyService.performAction(row.notificationKey, { id: "__click" })
    else if (actionId === "__attachment" && row.attachment && row.attachment.url)
      root.ntfyService.performAction(row.notificationKey, { id: "__attachment" })
  }

  function handleTextKey(text) {
    if (text === "r") {
      if (root.ntfyService) root.ntfyService.reload()
    } else if (text === "s") {
      root.editorOpen = true
    } else if (text === "/") {
      root.searchOpen = true
      Qt.callLater(function() { searchField.forceActiveFocus() })
    } else if (text === "o") root.openSpecial("__click")
    else if (text === "a") root.openSpecial("__attachment")
    else if (text === "e") root.toggleSelected()
    else if (text === "d") root.deleteSelected()
    else if (text === "1" || text === "2" || text === "3")
      root.performVisibleAction(Number(text) - 1)
  }

  onVisibleRowsChanged: {
    if (root.visibleRows.length === 0) {
      root.selectedIndex = 0
      root.expandedKey = ""
    } else root.selectedIndex = Math.max(0, Math.min(root.selectedIndex, root.visibleRows.length - 1))
  }

  onOpenedChanged: {
    if (opened) {
      root.selectedIndex = 0
      root.expandedKey = ""
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
    } else {
      serverMorePopup.close()
      clearPopup.close()
    }
  }

  Timer {
    interval: 60000
    repeat: true
    running: root.opened
    onTriggered: root.nowMs = Date.now()
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(600))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: searchField.activeFocus || root.editorOpen || serverMorePopup.opened
        || clearPopup.opened
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onActivateRequested: root.markSelectedRead()
      onDeleteRequested: root.deleteSelected()
      onTextKey: function(text) { root.handleTextKey(text) }
      onCloseRequested: {
        if (root.searchOpen) {
          searchField.text = ""
          root.searchOpen = false
        } else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }


      Shortcut {
        sequence: "Delete"
        enabled: root.opened && !root.nestedOpen
        onActivated: root.deleteSelected()
      }
      Column {
        id: content
        width: parent.width
        spacing: Style.space(10)

        Row {
          width: parent.width
          spacing: Style.space(8)

          Item {
            width: Style.space(28)
            height: width

            Image {
              id: headerMark
              anchors.fill: parent
              source: Qt.resolvedUrl("assets/ntfy-mask.svg")
              fillMode: Image.PreserveAspectFit
              visible: false
              layer.enabled: true
            }

            MultiEffect {
              anchors.fill: headerMark
              source: headerMark
              brightness: 1.0
              colorization: 1.0
              colorizationColor: Color.accent
            }
          }

          Column {
            width: parent.width - headerActions.implicitWidth - Style.space(36)
            spacing: Style.space(1)

            Text {
              text: "Notifications"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              width: parent.width
              text: Model.serverSummary(root.servers,
                root.ntfyService ? root.ntfyService.unreadCount : 0)
              color: Util.alpha(root.contentForeground, 0.62)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Row {
            id: headerActions
            spacing: Style.space(4)

            Button {
              text: "↻"
              onClicked: if (root.ntfyService) root.ntfyService.reload()
            }
            Button {
              text: "Settings"
              onClicked: root.editorOpen = true
            }
            Button {
              text: root.ntfyService && root.ntfyService.batchAction === "read_all" ? "Read all" : "Delete all"
              onClicked: {
                if (root.ntfyService && root.ntfyService.batchAction === "read_all") {
                  root.ntfyService.markRead(root.selectedServerId)
                } else {
                  clearPopup.open()
                }
              }
            }
          }
        }

        TextField {
          id: searchField
          width: parent.width
          visible: root.searchOpen
          placeholderText: "Search label, topic, title, body, or tags"
          Keys.onEscapePressed: function(event) {
            if (text) text = ""
            else root.searchOpen = false
            keyCatcher.forceActiveFocus()
            event.accepted = true
          }
        }

        Text {
          width: parent.width
          visible: root.ntfyService && root.ntfyService.lastError
          text: String(root.ntfyService ? root.ntfyService.lastError : "")
          color: Color.urgent
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
          maximumLineCount: 2
          elide: Text.ElideRight
        }

        Item {
          width: parent.width
          implicitHeight: root.editorOpen ? Style.space(500) : feedContent.implicitHeight

          ServerEditor {
            id: serverEditor
            anchors.fill: parent
            visible: root.editorOpen
            focus: visible
            service: root.ntfyService
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onDone: {
              root.editorOpen = false
              keyCatcher.forceActiveFocus()
            }
            onServerDeleted: function(serverId) {
              if (root.selectedServerId === serverId) root.selectServer("all")
            }
            onServerSelected: function(serverId) {
              root.selectServer(serverId)
              root.editorOpen = false
              Qt.callLater(function() { keyCatcher.forceActiveFocus() })
            }
            Keys.onEscapePressed: function(event) {
              root.editorOpen = false
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
          }

          Column {
            id: feedContent
            visible: !root.editorOpen
            width: parent.width
            spacing: Style.space(8)

            Row {
              width: parent.width
              spacing: Style.space(5)

              Repeater {
                model: root.tabState.tabs
                delegate: Button {
                  required property var modelData
                  text: String(modelData.label || "Server")
                    + (root.unreadFor(modelData.id) > 0 ? " " + root.unreadFor(modelData.id) : "")
                  selected: root.selectedServerId === modelData.id
                  onClicked: root.selectServer(modelData.id)
                }
              }

              Button {
                visible: root.tabState.more.length > 0
                text: "More"
                onClicked: serverMorePopup.open()
              }
            }

            PanelSeparator { width: parent.width }

            Text {
              width: parent.width
              visible: root.servers.length === 0
              text: "Add a server to begin"
              horizontalAlignment: Text.AlignHCenter
              color: Util.alpha(root.contentForeground, 0.72)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.body
            }

            Button {
              anchors.horizontalCenter: parent.horizontalCenter
              visible: root.servers.length === 0
              text: "Settings"
              onClicked: root.editorOpen = true
            }

            Text {
              width: parent.width
              visible: root.servers.length > 0 && root.visibleRows.length === 0
              text: root.searchQuery ? "No matches" : "Waiting for notifications"
              horizontalAlignment: Text.AlignHCenter
              color: Util.alpha(root.contentForeground, 0.72)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.body
            }

            ListView {
              id: feed
              width: parent.width
              height: root.visibleRows.length > 0
                ? Math.min(contentHeight, Style.space(430)) : 0
              clip: true
              spacing: Style.space(4)
              model: root.visibleRows
              currentIndex: root.selectedIndex
              boundsBehavior: Flickable.StopAtBounds
              ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

              delegate: NotificationRow {
                required property var modelData
                required property int index
                width: feed.width
                notification: modelData
                rowIndex: index
                expanded: root.expandedKey === modelData.notificationKey
                showServer: root.selectedServerId === "all"
                current: root.selectedIndex === index
                service: root.ntfyService
                actionsEnabled: root.ntfyService ? root.ntfyService.helperReady : false
                allowHttpActions: {
                  var server = root.serverFor(modelData.serverId)
                  return server ? server.allowHttpActions === true : false
                }
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                nowMs: root.nowMs
                onHovered: function(rowIndex) { root.selectedIndex = rowIndex }
                onActivated: function(rowIndex) {
                  root.selectedIndex = rowIndex
                  root.activateSelected()
                }
                onExpandRequested: function(rowIndex) {
                  root.selectedIndex = rowIndex
                  root.toggleSelected()
                }
                onMarkReadRequested: function(notificationKey) {
                  if (root.ntfyService) root.ntfyService.markNotificationRead(notificationKey)
                }
                onDeleteRequested: function(notificationKey) {
                  if (root.ntfyService) root.ntfyService.deleteNotification(notificationKey)
                }
                onActionRequested: function(notificationKey, action) {
                  if (root.ntfyService) root.ntfyService.performAction(notificationKey, action)
                }
                onMediaRequested: function(notificationKey, kind) {
                  if (root.ntfyService) root.ntfyService.fetchMedia(notificationKey, kind)
                }
              }
            }
          }
        }
      }
    }
  }


  Popup {
    id: serverMorePopup
    parent: keyCatcher
    anchors.centerIn: parent
    modal: false
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    contentItem: Column {
      Repeater {
        model: root.tabState.more
        delegate: Button {
          required property var modelData
          text: String(modelData.label || "Server")
            + (root.unreadFor(modelData.id) > 0 ? " " + root.unreadFor(modelData.id) : "")
          onClicked: root.selectServer(modelData.id)
        }
      }
    }
  }

  Popup {
    id: clearPopup
    parent: keyCatcher
    anchors.centerIn: parent
    modal: true
    closePolicy: Popup.CloseOnEscape
    contentItem: Column {
      spacing: Style.space(8)
      Text {
        text: "Delete all notifications in current view?"
        color: root.contentForeground
        font.family: root.contentFontFamily
        font.pixelSize: Style.font.body
      }
      Row {
        spacing: Style.space(8)
        Button {
          text: "Cancel"
          onClicked: clearPopup.close()
        }
        Button {
          text: "Delete all"
          onClicked: {
            if (root.ntfyService) root.ntfyService.clear(root.selectedServerId)
            clearPopup.close()
          }
        }
      }
    }
  }
}
