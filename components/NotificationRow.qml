import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../Model.js" as Model

CursorSurface {
  id: root

  required property var notification
  required property int rowIndex
  property bool expanded: false
  property bool showServer: false
  property bool actionsEnabled: false
  property bool allowHttpActions: false
  property var service: null
  property string fontFamily: Style.font.family
  property double nowMs: Date.now()

  signal hovered(int rowIndex)
  signal activated(int rowIndex)
  signal expandRequested(int rowIndex)
  signal markReadRequested(string notificationKey)
  signal deleteRequested(string notificationKey)
  signal actionRequested(string notificationKey, var action)
  signal mediaRequested(string notificationKey, string kind)

  readonly property var priority: Model.priorityMeta(notification.priority)
  readonly property var attachment: notification.attachment || ({})
  readonly property bool hasAttachment: !!attachment.url
  readonly property var notificationActions: notification.actions || []
  readonly property bool attachmentExpired: Number(attachment.expires || notification.expires || 0) > 0
    && Number(attachment.expires || notification.expires) <= Math.floor(Date.now() / 1000)
  readonly property var iconMediaState: service
    ? service.mediaState(notification.notificationKey, "icon") : ({ state: "idle", text: "" })
  readonly property var attachmentMediaState: service
    ? service.mediaState(notification.notificationKey, "attachment") : ({ state: "idle", text: "" })
  // Landscape images (video and article thumbnails) read better as a full-width
  // card banner than squeezed into the icon slot; square avatars stay icons.
  readonly property bool cardView: iconImage.status === Image.Ready
    && iconImage.implicitWidth > iconImage.implicitHeight * 1.15

  function requestIcon() {
    if (root.visible && root.notification.icon && !root.notification.iconPath
        && root.iconMediaState.state === "idle")
      root.mediaRequested(root.notification.notificationKey, "icon")
  }

  function requestAttachment() {
    if (root.expanded && root.hasAttachment && !root.attachmentExpired
        && !root.notification.attachmentPath && root.attachmentMediaState.state === "idle")
      root.mediaRequested(root.notification.notificationKey, "attachment")
  }

  function fileSize(value) {
    var bytes = Number(value || 0)
    if (!bytes) return ""
    if (bytes < 1024) return bytes + " B"
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KiB"
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB"
  }

  implicitHeight: body.implicitHeight + Style.space(16)
  foreground: root.foreground
  hasCursor: current

  onVisibleChanged: requestIcon()
  onExpandedChanged: requestAttachment()
  Component.onCompleted: requestIcon()

  HoverHandler {
    onHoveredChanged: if (hovered) root.hovered(root.rowIndex)
  }

  TapHandler {
    acceptedButtons: Qt.LeftButton
    onTapped: root.activated(root.rowIndex)
  }

  Column {
    id: body
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.margins: Style.space(8)
    spacing: Style.space(7)

    Image {
      width: parent.width
      height: visible ? Math.round(width * 9 / 16) : 0
      visible: root.cardView
      source: visible ? iconImage.source : ""
      fillMode: Image.PreserveAspectCrop
      asynchronous: true
      clip: true
    }

    Row {
      width: parent.width
      spacing: root.cardView ? 0 : Style.space(9)

      Item {
        visible: !root.cardView
        width: visible ? Style.space(40) : 0
        height: Style.space(40)

        Image {
          id: iconImage
          anchors.fill: parent
          source: root.notification.iconPath ? Util.fileUrl(root.notification.iconPath) : ""
          fillMode: Image.PreserveAspectFit
          asynchronous: true
          visible: status === Image.Ready
        }

        Text {
          anchors.centerIn: parent
          visible: iconImage.status !== Image.Ready
          text: root.priority.glyph
          color: root.priority.urgent ? Color.urgent
            : (root.notification.unread ? Color.accent : Util.alpha(root.foreground, 0.68))
          font.family: root.fontFamily
          font.pixelSize: Style.space(22)
        }
      }

      Column {
        width: parent.width - (root.cardView ? 0 : Style.space(49))
        spacing: Style.space(2)

        Row {
          width: parent.width
          spacing: Style.space(6)

          Rectangle {
            visible: root.notification.unread
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(5)
            height: width
            radius: width / 2
            color: root.priority.urgent ? Color.urgent : Color.accent
          }

          Text {
            id: titleLabel
            width: parent.width - timeLabel.implicitWidth
              - (root.notification.unread ? Style.space(11) : Style.space(5))
              - (rowActions.visible ? rowActions.implicitWidth + Style.space(6) : 0)
            text: String(root.notification.title || root.notification.message || "Notification")
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: root.notification.unread
            elide: Text.ElideRight
            maximumLineCount: 1
          }

          Row {
            id: rowActions
            visible: root.current
            spacing: Style.space(1)

            // Tapping a row with a link opens it, so expanding needs its own control.
            PanelActionButton {
              visible: !!root.notification.click
              size: Style.space(20)
              iconText: root.expanded ? "⌃" : "⌄"
              tooltipText: root.expanded ? "Collapse" : "Expand"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.expandRequested(root.rowIndex)
            }

            PanelActionButton {
              size: Style.space(20)
              iconText: "✓"
              tooltipText: root.notification.unread ? "Mark as read" : "Already read"
              enabled: root.notification.unread
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.markReadRequested(root.notification.notificationKey)
            }

            PanelActionButton {
              size: Style.space(20)
              iconText: "×"
              tooltipText: "Delete"
              foreground: root.foreground
              hoverColor: Color.urgent
              fontFamily: root.fontFamily
              onClicked: root.deleteRequested(root.notification.notificationKey)
            }
          }

          Text {
            id: timeLabel
            text: Model.relativeTime(root.notification.time, root.nowMs)
            color: Util.alpha(root.foreground, 0.55)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Text {
          width: parent.width
          text: String(root.notification.message || "")
          visible: text.length > 0
          color: Util.alpha(root.foreground, 0.78)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          maximumLineCount: 1
        }

        Text {
          width: parent.width
          text: String(root.notification.topic || "")
            + (root.showServer ? " · " + String(root.notification.serverLabel || "") : "")
          color: Util.alpha(root.foreground, 0.55)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          maximumLineCount: 1
        }
      }
    }

    Column {
      visible: root.expanded
      width: parent.width
      spacing: Style.space(7)

      Text {
        width: parent.width
        visible: String(root.notification.message || "").length > 0
        text: String(root.notification.message || "")
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        maximumLineCount: 6
        elide: Text.ElideRight
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        width: parent.width
        visible: Array.isArray(root.notification.tags) && root.notification.tags.length > 0
        text: root.notification.tags.map(function(tag) { return "#" + tag }).join("  ")
        color: Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
      }

      Image {
        width: Math.min(parent.width, Style.space(320))
        height: visible ? Math.min(implicitHeight, Style.space(180)) : 0
        source: root.notification.attachmentPath ? Util.fileUrl(root.notification.attachmentPath) : ""
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        // The card banner already shows it when icon and attachment are one image.
        visible: status === Image.Ready && !(root.cardView && root.attachment.url === root.notification.icon)
      }

      Text {
        width: parent.width
        visible: root.hasAttachment && !root.notification.attachmentPath
        text: String(root.attachment.name || "Attachment")
          + (root.fileSize(root.attachment.size) ? " · " + root.fileSize(root.attachment.size) : "")
          + (root.attachmentExpired ? " · Expired"
             : (root.attachmentMediaState.state === "error" ? " · " + root.attachmentMediaState.text : ""))
        color: Util.alpha(root.foreground, 0.68)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      Row {
        visible: !!root.notification.click || root.hasAttachment
        spacing: Style.space(7)

        Button {
          visible: !!root.notification.click
          text: "Open"
          enabled: root.actionsEnabled
          onClicked: root.actionRequested(root.notification.notificationKey,
            { id: "__click", action: "view", label: "Open" })
        }

        Button {
          visible: root.hasAttachment
          text: "Open attachment"
          enabled: root.actionsEnabled && !root.attachmentExpired
          onClicked: root.actionRequested(root.notification.notificationKey,
            { id: "__attachment", action: "view", label: "Open attachment" })
        }
      }

      Repeater {
        model: root.notificationActions
        delegate: Column {
          required property var modelData
          width: parent.width
          spacing: Style.space(2)
          readonly property bool trusted: modelData.action !== "http" || root.allowHttpActions

          Text {
            visible: parent.modelData.action === "http"
            text: parent.trusted ? Model.actionDisplay(parent.modelData)
              : "Publisher-supplied HTTP actions are disabled"
            color: Util.alpha(root.foreground, 0.58)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }

    // Publisher actions stay on the card, as on Android, so they work without
    // expanding the row. Untrusted HTTP actions stay clickable and report why
    // they were refused inline, like any other failure.
    Flow {
      width: parent.width
      visible: root.notificationActions.length > 0
      spacing: Style.space(7)

      Repeater {
        model: root.notificationActions
        delegate: Button {
          required property var modelData
          readonly property var actionState: root.service
            ? root.service.actionState(root.notification.notificationKey, modelData.id)
            : ({ state: "idle", text: "" })

          text: (actionState.state === "working" ? "Working"
            : (actionState.state === "done" ? "Done" : String(modelData.label || "Action"))).toUpperCase()
          enabled: root.actionsEnabled && actionState.state !== "working"
          onClicked: root.actionRequested(root.notification.notificationKey, modelData)
        }
      }
    }

    Repeater {
      model: root.notificationActions
      delegate: Text {
        required property var modelData
        readonly property var actionState: root.service
          ? root.service.actionState(root.notification.notificationKey, modelData.id)
          : ({ state: "idle", text: "" })
        width: parent.width
        visible: actionState.state === "error"
        text: String(modelData.label || "Action") + " failed: " + actionState.text
        color: Color.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
        maximumLineCount: 2
        elide: Text.ElideRight
      }
    }
  }
}
