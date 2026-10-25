import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

Item {
  id: root

  property var shell: null
  property var servers: []
  property var notifications: []
  property int unreadCount: 0
  property int urgentUnreadCount: 0
  property string overallStatus: "starting"
  property string lastError: ""
  property bool helperReady: false
  property bool shuttingDown: false
  property int nextRequestNumber: 1
  property int restartDelayMs: 1000
  property var actionStates: ({})
  property var mediaStates: ({})
  property var toastSettings: ({ enabled: false, duration: "default" })
  property string batchAction: "delete_all"
  property double muteUntil: 0
  property double nowMs: Date.now()
  readonly property bool dndActive: muteUntil < 0 || muteUntil * 1000 > nowMs

  signal configCompleted(var result)
  signal testCompleted(var result)

  readonly property string helperPath: {
    var url = String(Qt.resolvedUrl("ntfy_bridge.py"))
    return decodeURIComponent(url.indexOf("file://") === 0 ? url.substring(7) : url)
  }

  function requestId() {
    var value = "r" + root.nextRequestNumber
    root.nextRequestNumber += 1
    return value
  }

  function send(command) {
    if (!bridgeProcess.running || !root.helperReady) return ""
    if (!command.requestId) command.requestId = root.requestId()
    bridgeProcess.write(JSON.stringify(command) + "\n")
    return command.requestId
  }

  function updateDerived() {
    var unread = 0
    var urgent = 0
    for (var index = 0; index < root.notifications.length; index++) {
      var row = root.notifications[index]
      if (row && row.unread) {
        unread += 1
        if (Number(row.priority) === 5) urgent += 1
      }
    }
    root.unreadCount = unread
    root.urgentUnreadCount = urgent

    if (!root.helperReady) {
      root.overallStatus = "error"
      return
    }
    var enabled = root.servers.filter(function(server) { return server.enabled !== false })
    if (root.servers.length === 0) root.overallStatus = "empty"
    else if (enabled.length === 0) root.overallStatus = "disabled"
    else {
      var connected = enabled.filter(function(server) { return server.state === "connected" }).length
      var failed = enabled.filter(function(server) {
        return server.state === "auth-error" || server.state === "backoff"
      }).length
      root.overallStatus = connected === enabled.length ? "connected"
        : (connected > 0 ? "partial" : (failed > 0 ? "error" : "connecting"))
    }
  }

  function applySnapshot(message) {
    root.servers = Array.isArray(message.servers) ? message.servers.slice() : []
    root.notifications = Array.isArray(message.notifications) ? message.notifications.slice() : []
    var toastSettings = message.toastSettings
    root.toastSettings = toastSettings && typeof toastSettings === "object"
      ? {
          enabled: toastSettings.enabled === true,
          duration: String(toastSettings.duration || "default")
        }
      : { enabled: false, duration: "default" }
    root.batchAction = String(message.batchAction || "delete_all")
    root.nowMs = Date.now()
    root.muteUntil = Number(message.muteUntil || 0)
    root.helperReady = true
    root.restartDelayMs = 1000
    root.lastError = ""
    root.pruneTransientStates()
    root.updateDerived()
  }

  function pruneTransientStates() {
    var valid = Object.create(null)
    for (var i = 0; i < root.notifications.length; i++) {
      var row = root.notifications[i]
      if (row && row.notificationKey) valid[String(row.notificationKey)] = true
    }
    var nextActions = ({})
    for (var aKey in root.actionStates) {
      var aPrefix = aKey.split("\u0000", 1)[0]
      if (valid[aPrefix]) nextActions[aKey] = root.actionStates[aKey]
    }
    root.actionStates = nextActions
    var nextMedia = ({})
    for (var mKey in root.mediaStates) {
      var mPrefix = mKey.split("\u0000", 1)[0]
      if (valid[mPrefix]) nextMedia[mKey] = root.mediaStates[mKey]
    }
    root.mediaStates = nextMedia
  }

  function upsertNotification(notification) {
    if (!notification || !notification.notificationKey) return
    var next = root.notifications.slice()
    var found = -1
    for (var index = 0; index < next.length; index++) {
      if (next[index].notificationKey === notification.notificationKey) {
        found = index
        break
      }
    }
    if (found >= 0) next[found] = notification
    else next.push(notification)
    root.notifications = next
    root.updateDerived()
  }

  function removeNotification(notificationKey) {
    root.notifications = root.notifications.filter(function(row) {
      return row.notificationKey !== notificationKey
    })
    root.pruneTransientStates()
    root.updateDerived()
  }

  function updateServerStatus(message) {
    var next = root.servers.slice()
    for (var index = 0; index < next.length; index++) {
      if (next[index].id !== message.serverId) continue
      var server = Object.assign({}, next[index])
      server.state = String(message.state || "connecting")
      server.error = String(message.error || "")
      next[index] = server
      root.servers = next
      root.updateDerived()
      return
    }
  }

  function setActionState(notificationKey, actionId, state, text) {
    var key = String(notificationKey) + "\u0000" + String(actionId)
    var next = Object.assign({}, root.actionStates)
    next[key] = { state: state, text: text }
    root.actionStates = next
  }

  function actionState(notificationKey, actionId) {
    var key = String(notificationKey) + "\u0000" + String(actionId)
    return root.actionStates[key] || { state: "idle", text: "" }
  }

  function setMediaState(notificationKey, kind, state, text) {
    var key = String(notificationKey) + "\u0000" + String(kind)
    var next = Object.assign({}, root.mediaStates)
    next[key] = { state: state, text: text }
    root.mediaStates = next
  }

  function mediaState(notificationKey, kind) {
    var key = String(notificationKey) + "\u0000" + String(kind)
    return root.mediaStates[key] || { state: "idle", text: "" }
  }

  function applyMedia(message) {
    var state = message.ok ? "done" : "error"
    root.setMediaState(message.notificationKey, message.kind, state,
                       message.ok ? "" : String(message.error || "Media unavailable"))
    if (!message.ok) return
    var next = root.notifications.slice()
    for (var index = 0; index < next.length; index++) {
      if (next[index].notificationKey !== message.notificationKey) continue
      var row = Object.assign({}, next[index])
      row[message.kind === "icon" ? "iconPath" : "attachmentPath"] = String(message.path || "")
      next[index] = row
      root.notifications = next
      return
    }
  }

  function handleLine(line) {
    var message = Model.parseLine(line)
    if (!message || !message.event) {
      root.lastError = "Ignored malformed helper output"
      return
    }
    if (message.event === "snapshot") root.applySnapshot(message)
    else if (message.event === "notification_upsert") root.upsertNotification(message.notification)
    else if (message.event === "notification_remove") root.removeNotification(message.notificationKey)
    else if (message.event === "server_status") root.updateServerStatus(message)
    else if (message.event === "mute_status") {
      root.nowMs = Date.now()
      root.muteUntil = Number(message.muteUntil || 0)
    }
    else if (message.event === "action_result") {
      root.setActionState(message.notificationKey, message.actionId,
                          message.ok ? "done" : "error",
                          message.ok ? "Done" : String(message.error || "Action failed"))
    } else if (message.event === "media_result") root.applyMedia(message)
    else if (message.event === "config_result") root.configCompleted(message)
    else if (message.event === "test_result") root.testCompleted(message)
    else if (message.event === "error") {
      root.lastError = String(message.message || "Helper error").slice(0, 512)
      if (message.fatal) root.overallStatus = "error"
    }
  }

  function markRead(serverId) {
    return root.send({ cmd: "mark_read", serverId: String(serverId || "all") })
  }

  function markNotificationRead(notificationKey) {
    return root.send({
      cmd: "mark_notification_read",
      notificationKey: String(notificationKey || "")
    })
  }

  function deleteNotification(notificationKey) {
    return root.send({
      cmd: "delete_notification",
      notificationKey: String(notificationKey || "")
    })
  }

  function clear(serverId) {
    return root.send({ cmd: "clear", serverId: String(serverId || "all") })
  }

  function setMute(durationSeconds) {
    return root.send({ cmd: "set_mute", durationSeconds: Number(durationSeconds || 0) })
  }

  function performAction(notificationKey, action) {
    if (!root.helperReady || !action || !action.id) return ""
    var current = root.actionState(notificationKey, action.id)
    if (current.state === "working") return ""
    var id = root.requestId()
    root.setActionState(notificationKey, action.id, "working", "Working")
    bridgeProcess.write(JSON.stringify({ cmd: "perform_action", requestId: id,
      notificationKey: String(notificationKey), actionId: String(action.id) }) + "\n")
    return id
  }

  function fetchMedia(notificationKey, kind) {
    if (!root.helperReady) return ""
    var current = root.mediaState(notificationKey, kind)
    if (current.state === "working" || current.state === "done") return ""
    var id = root.requestId()
    root.setMediaState(notificationKey, kind, "working", "Loading")
    bridgeProcess.write(JSON.stringify({ cmd: "fetch_media", requestId: id,
      notificationKey: String(notificationKey), kind: String(kind) }) + "\n")
    return id
  }

  function openUrl(url) {
    if (!root.helperReady || !String(url || "")) return ""
    return root.send({ cmd: "open_url", url: String(url) })
  }

  function copyText(text) {
    if (!root.helperReady || !String(text || "")) return ""
    return root.send({ cmd: "copy_text", text: String(text) })
  }

  function saveServer(server) {
    return root.send({ cmd: "save_server", server: server })
  }

  function saveToastSettings(settings) {
    return root.send({ cmd: "save_toast_settings", settings: settings })
  }

  function saveBatchAction(action) {
    return root.send({ cmd: "save_batch_action", batchAction: String(action || "delete_all") })
  }

  function deleteServer(serverId) {
    return root.send({ cmd: "delete_server", serverId: String(serverId || "") })
  }

  function testServer(server) {
    return root.send({ cmd: "test_server", server: server })
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.muteUntil !== 0
    onTriggered: {
      root.nowMs = Date.now()
      if (root.muteUntil > 0 && root.muteUntil * 1000 <= root.nowMs) root.setMute(0)
    }
  }

  function reload() {
    return root.send({ cmd: "reload" })
  }

  Process {
    id: bridgeProcess
    command: ["/usr/bin/setpriv", "--pdeathsig", "TERM", "/usr/bin/python3", "-I", "-B", root.helperPath]
    stdinEnabled: true
    running: true

    stdout: SplitParser {
      onRead: function(line) { root.handleLine(line) }
    }

    stderr: SplitParser {
      onRead: function(line) {
        var text = String(line || "").slice(0, 512)
        if (text) root.lastError = text
      }
    }

    onStarted: {
      root.helperReady = false
      root.overallStatus = "starting"
    }

    onExited: function(exitCode) {
      root.helperReady = false
      root.overallStatus = "error"
      if (!root.shuttingDown) {
        restartTimer.interval = root.restartDelayMs
        root.restartDelayMs = Math.min(30000, root.restartDelayMs * 2)
        restartTimer.restart()
      }
    }
  }

  Timer {
    id: restartTimer
    interval: 1000
    repeat: false
    onTriggered: {
      if (!root.shuttingDown) bridgeProcess.running = true
    }
  }

  Component.onDestruction: {
    root.shuttingDown = true
    restartTimer.stop()
    bridgeProcess.running = false
  }
}
