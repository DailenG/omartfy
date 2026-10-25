import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../Model.js" as Model

Item {
  id: root

  property var service: null
  property var servers: service ? service.servers : []
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property string editingId: ""
  property string pendingSave: ""
  property string pendingTest: ""
  property string pendingDelete: ""
  property string pendingToastSave: ""
  property string pendingBatchActionSave: ""
  property string resultText: ""
  property string toastResultText: ""
  property string batchActionResultText: ""
  property bool deleteConfirmation: false
  property bool addingServer: false
  readonly property var toastDurationValues: [
    "default", "8-seconds", "15-seconds", "30-seconds", "until-dismissed"
  ]
  readonly property var batchActionValues: [
    "delete_all", "read_all"
  ]

  signal done()
  signal serverDeleted(string serverId)
  signal serverSelected(string serverId)

  function selectSavedServer() {
    if (!root.visible || root.addingServer || root.editingId || root.servers.length === 0) return
    root.editServer(root.servers[0])
  }

  onVisibleChanged: {
    Qt.callLater(root.selectSavedServer)
    Qt.callLater(root.loadToastSettings)
    Qt.callLater(root.loadBatchAction)
  }
  onServersChanged: Qt.callLater(root.selectSavedServer)
  Component.onCompleted: {
    Qt.callLater(root.selectSavedServer)
    Qt.callLater(root.loadToastSettings)
    Qt.callLater(root.loadBatchAction)
  }

  function loadToastSettings() {
    var settings = root.service ? root.service.toastSettings : null
    nativeToastsCheck.checked = !!(settings && settings.enabled === true)
    var duration = String((settings || {}).duration || "default")
    var index = root.toastDurationValues.indexOf(duration)
    toastDurationBox.currentIndex = index >= 0 ? index : 0
  }

  function saveToastSettings() {
    if (!root.service || root.pendingToastSave) return
    root.toastResultText = ""
    root.pendingToastSave = root.service.saveToastSettings({
      enabled: nativeToastsCheck.checked,
      duration: root.toastDurationValues[toastDurationBox.currentIndex] || "default"
    })
  }

  function loadBatchAction() {
    var action = root.service ? root.service.batchAction : "delete_all"
    var index = root.batchActionValues.indexOf(action)
    batchActionBox.currentIndex = index >= 0 ? index : 0
  }

  function saveBatchAction() {
    if (!root.service || root.pendingBatchActionSave) return
    root.batchActionResultText = ""
    root.pendingBatchActionSave = root.service.saveBatchAction(
      root.batchActionValues[batchActionBox.currentIndex] || "delete_all"
    )
  }

  function topics() {
    return topicsField.text.split(",").map(function(value) { return value.trim() })
      .filter(function(value) { return value.length > 0 })
  }

  function authType() {
    return String(authTypeBox.currentText || "none")
  }

  function isLoopback(hostname) {
    var value = String(hostname || "").toLowerCase().replace(/\.$/, "")
    return value === "localhost" || value === "127.0.0.1" || value === "::1"
  }

  function insecureCredentials() {
    var parsed = Model.parseBaseUrl(baseUrlField.text)
    return parsed.valid && parsed.protocol === "http:"
      && root.authType() !== "none" && !root.isLoopback(parsed.hostname)
  }

  function validationError() {
    if (!labelField.text.trim()) return "Label is required"
    var parsed = Model.parseBaseUrl(baseUrlField.text)
    if (!parsed.valid) {
      if (parsed.error === "forbidden") return "URL cannot include credentials, query, or fragment"
      return "Enter an absolute HTTP or HTTPS URL"
    }
    var values = root.topics()
    if (values.length === 0) return "At least one topic is required"
    var seen = ({})
    for (var index = 0; index < values.length; index++) {
      if (!/^[-_A-Za-z0-9]{1,64}$/.test(values[index])) return "Topics use letters, numbers, underscore, or hyphen"
      if (seen[values[index]]) return "Topics must be unique"
      seen[values[index]] = true
    }
    if (root.authType() === "basic" && !usernameField.text) return "Username is required"
    if (root.authType() !== "none" && !root.editingId && !secretField.text) return "Secret is required"
    if (root.insecureCredentials() && !insecureCheck.checked) return "Acknowledge credentials over insecure HTTP"
    return ""
  }

  function candidate() {
    return {
      id: root.editingId,
      label: labelField.text.trim(),
      baseUrl: baseUrlField.text.trim(),
      topics: root.topics(),
      enabled: enabledCheck.checked,
      showToasts: showToastsCheck.checked,
      allowHttpActions: httpActionsCheck.checked,
      allowExternalMedia: externalMediaCheck.checked,
      allowInsecureHttp: insecureCheck.checked,
      auth: {
        type: root.authType(),
        username: usernameField.text,
        secret: secretField.text
      }
    }
  }

  function resetForm() {
    root.addingServer = false
    root.editingId = ""
    labelField.text = ""
    baseUrlField.text = "https://ntfy.sh"
    topicsField.text = ""
    enabledCheck.checked = true
    showToastsCheck.checked = true
    httpActionsCheck.checked = false
    externalMediaCheck.checked = false
    insecureCheck.checked = false
    authTypeBox.currentIndex = 0
    usernameField.text = ""
    secretField.text = ""
    root.resultText = ""
    root.deleteConfirmation = false
  }

  function beginAdd() {
    root.resetForm()
    root.addingServer = true
  }

  function editServer(server) {
    root.addingServer = false
    root.editingId = String(server.id || "")
    labelField.text = String(server.label || "")
    baseUrlField.text = String(server.baseUrl || "")
    topicsField.text = Array.isArray(server.topics) ? server.topics.join(", ") : ""
    enabledCheck.checked = server.enabled !== false
    showToastsCheck.checked = server.showToasts !== false
    httpActionsCheck.checked = server.allowHttpActions === true
    externalMediaCheck.checked = server.allowExternalMedia === true
    insecureCheck.checked = server.allowInsecureHttp === true
    var type = String((server.auth || {}).type || "none")
    authTypeBox.currentIndex = Math.max(0, ["none", "token", "basic"].indexOf(type))
    usernameField.text = String((server.auth || {}).username || "")
    secretField.text = ""
    root.resultText = ""
    root.deleteConfirmation = false
    formFlick.contentY = Math.max(0, formColumn.y - Style.space(8))
  }

  function save() {
    if (!root.service || root.validationError()) return
    root.resultText = ""
    root.pendingSave = root.service.saveServer(root.candidate())
  }

  function test() {
    if (!root.service || root.validationError()) return
    root.resultText = "Testing…"
    root.pendingTest = root.service.testServer(root.candidate())
  }

  Connections {
    target: root.service
    function onToastSettingsChanged() {
      root.loadToastSettings()
    }
    function onBatchActionChanged() {
      root.loadBatchAction()
    }


    function onConfigCompleted(result) {
      if (result.requestId === root.pendingToastSave && result.operation === "save_toasts") {
        root.pendingToastSave = ""
        root.toastResultText = result.ok
          ? "Saved"
          : String(result.error || "Could not save notification settings")
        return
      }
      if (result.requestId === root.pendingBatchActionSave && result.operation === "save_batch_action") {
        root.pendingBatchActionSave = ""
        root.batchActionResultText = result.ok
          ? "Saved"
          : String(result.error || "Could not save header action setting")
        return
      }
      if (result.requestId !== root.pendingSave && result.requestId !== root.pendingDelete) return
      if (result.ok) {
        if (result.operation === "delete") {
          root.serverDeleted(String(result.deletedServerId || ""))
          root.resetForm()
        } else {
          root.pendingSave = ""
          root.resultText = "Saved"
          root.editingId = String((result.server || {}).id || root.editingId)
          root.addingServer = false
          secretField.text = ""
        }
      } else {
        root.resultText = String(result.error || "Could not save")
      }
      root.pendingSave = ""
      root.pendingDelete = ""
      root.deleteConfirmation = false
    }

    function onTestCompleted(result) {
      if (result.requestId !== root.pendingTest) return
      root.pendingTest = ""
      root.resultText = result.ok ? "Connection succeeded" : String(result.error || "Connection failed")
    }
  }

  Flickable {
    id: formFlick
    anchors.fill: parent
    contentWidth: width
    contentHeight: content.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    flickableDirection: Flickable.VerticalFlick
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    Column {
      id: content
      width: formFlick.width
      spacing: Style.space(10)

      Row {
        width: parent.width
        spacing: Style.space(8)

        Button {
          text: "Back"
          onClicked: root.done()
        }

        Text {
          anchors.verticalCenter: parent.verticalCenter
          text: "Settings"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
        }

        Item { width: Math.max(0, parent.width - Style.space(190)); height: 1 }

        Button {
          text: "Add"
          onClicked: root.beginAdd()
        }
      }

      Column {
        width: parent.width
        spacing: Style.space(7)

        Text {
          text: "Desktop notifications"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
        }

        CheckBox {
          id: nativeToastsCheck
          text: "Show native Omarchy toasts"
        }

        Row {
          spacing: Style.space(8)

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Duration"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          ComboBox {
            id: toastDurationBox
            enabled: nativeToastsCheck.checked
            model: [
              "Omarchy default",
              "8 seconds",
              "15 seconds",
              "30 seconds",
              "Until dismissed"
            ]
          }
        }

        Button {
          text: root.pendingToastSave ? "Saving…" : "Save notification settings"
          enabled: !!root.service && !root.pendingToastSave
          onClicked: root.saveToastSettings()
        }

        Text {
          width: parent.width
          visible: root.toastResultText.length > 0
          text: root.toastResultText
          color: root.toastResultText === "Saved" ? Color.accent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        Text {
          width: parent.width
          text: "Omarchy default follows ntfy priority. Finite choices use the exact lifetime; until dismissed requires manual close. Omartfy DND and Omarchy notification DND suppress popups while the inbox keeps collecting."
          color: root.foreground
          opacity: 0.72
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }
      }

      PanelSeparator { width: parent.width }

      Column {
        width: parent.width
        spacing: Style.space(7)

        Text {
          text: "Header action"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
        }

        Row {
          spacing: Style.space(8)

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Action button"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          ComboBox {
            id: batchActionBox
            model: [
              "Delete all",
              "Read all"
            ]
          }
        }

        Button {
          text: root.pendingBatchActionSave ? "Saving…" : "Save header action"
          enabled: !!root.service && !root.pendingBatchActionSave
          onClicked: root.saveBatchAction()
        }

        Text {
          width: parent.width
          visible: root.batchActionResultText.length > 0
          text: root.batchActionResultText
          color: root.batchActionResultText === "Saved" ? Color.accent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        Text {
          width: parent.width
          text: "Controls whether the panel header button deletes all notifications in the active view (with confirmation) or marks them all as read."
          color: root.foreground
          opacity: 0.72
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }
      }
      PanelSeparator { width: parent.width }

      Column {
        width: parent.width
        spacing: Style.space(4)
        visible: root.servers.length > 0

        Repeater {
          model: root.servers
          delegate: Row {
            id: serverRow
            required property var modelData
            width: content.width
            spacing: Style.space(4)

            Button {
              width: Math.max(0, serverRow.width - editButton.implicitWidth - serverRow.spacing)
              text: String(serverRow.modelData.label || "Server") + "  ·  "
                + String(serverRow.modelData.state
                  || (serverRow.modelData.enabled ? "connecting" : "disabled"))
              onClicked: root.serverSelected(String(serverRow.modelData.id || ""))
            }

            PanelActionButton {
              id: editButton
              iconText: "✎"
              tooltipText: "Edit server"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.editServer(serverRow.modelData)
            }
          }
        }
      }

      PanelSeparator { width: parent.width }

      Column {
        id: formColumn
        width: parent.width
        spacing: Style.space(7)

        Text {
          text: root.editingId ? "Edit server" : "Add server"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
        }

        TextField {
          id: labelField
          width: parent.width
          placeholderText: "Label"
        }

        TextField {
          id: baseUrlField
          width: parent.width
          placeholderText: "https://ntfy.example.com"
          text: "https://ntfy.sh"
        }

        TextField {
          id: topicsField
          width: parent.width
          placeholderText: "alerts, backups"
        }

        Row {
          spacing: Style.space(12)
          CheckBox { id: enabledCheck; text: "Enabled"; checked: true }
          CheckBox { id: httpActionsCheck; text: "Allow publisher-supplied HTTP actions" }
        }

        CheckBox {
          id: externalMediaCheck
          width: parent.width
          text: "Load images from other hosts"
        }

        CheckBox {
          id: showToastsCheck
          width: parent.width
          text: "Show toasts from this server"
          checked: true
        }

        Row {
          spacing: Style.space(8)
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Authentication"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
          ComboBox {
            id: authTypeBox
            model: ["none", "token", "basic"]
          }
        }

        TextField {
          id: usernameField
          width: parent.width
          visible: root.authType() === "basic"
          placeholderText: "Username"
        }

        TextField {
          id: secretField
          width: parent.width
          visible: root.authType() !== "none"
          echoMode: TextInput.Password
          placeholderText: root.editingId ? "Secret (blank preserves current)"
            : (root.authType() === "token" ? "Access token" : "Password")
        }

        CheckBox {
          id: insecureCheck
          width: parent.width
          visible: root.insecureCredentials()
          text: "Allow credentials over insecure non-loopback HTTP"
        }

        Text {
          width: parent.width
          visible: root.validationError().length > 0
          text: root.validationError()
          color: Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        Text {
          width: parent.width
          visible: root.resultText.length > 0
          text: root.resultText
          color: root.resultText === "Saved" || root.resultText === "Connection succeeded"
            ? Color.accent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        Row {
          spacing: Style.space(8)

          Button {
            text: root.pendingTest ? "Testing…" : "Test"
            enabled: !root.validationError() && !root.pendingSave && !root.pendingTest
            onClicked: root.test()
          }

          Button {
            text: root.pendingSave ? "Saving…" : "Save"
            enabled: !root.validationError() && !root.pendingSave && !root.pendingTest
            onClicked: root.save()
          }

          Button {
            visible: !!root.editingId
            text: root.deleteConfirmation ? "Confirm delete" : "Delete"
            enabled: !root.pendingSave && !root.pendingDelete
            onClicked: {
              if (!root.deleteConfirmation) root.deleteConfirmation = true
              else if (root.service) root.pendingDelete = root.service.deleteServer(root.editingId)
            }
          }

          Button {
            visible: root.deleteConfirmation
            text: "Cancel"
            onClicked: root.deleteConfirmation = false
          }
        }
      }
    }
  }
}
