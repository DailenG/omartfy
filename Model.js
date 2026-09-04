.pragma library

function parseLine(line) {
  try {
    var value = JSON.parse(String(line || ""))
    return value && typeof value === "object" && !Array.isArray(value) ? value : null
  } catch (error) {
    return null
  }
}

function parseBaseUrl(value) {
  var text = String(value || "").trim()
  var scheme = /^(https?):\/\//i.exec(text)
  if (!scheme) return { valid: false, error: "absolute", protocol: "", hostname: "" }

  var remainder = text.substring(scheme[0].length)
  var boundary = remainder.search(/[\/?#]/)
  var authority = boundary < 0 ? remainder : remainder.substring(0, boundary)
  if (!authority) return { valid: false, error: "absolute", protocol: "", hostname: "" }
  if (authority.indexOf("@") >= 0 || text.indexOf("?") >= 0 || text.indexOf("#") >= 0)
    return { valid: false, error: "forbidden", protocol: "", hostname: "" }

  var hostname = authority
  if (authority.charAt(0) === "[") {
    var closingBracket = authority.indexOf("]")
    if (closingBracket < 1)
      return { valid: false, error: "absolute", protocol: "", hostname: "" }
    hostname = authority.substring(1, closingBracket)
  } else {
    var portSeparator = authority.lastIndexOf(":")
    if (portSeparator >= 0) hostname = authority.substring(0, portSeparator)
  }
  if (!hostname)
    return { valid: false, error: "absolute", protocol: "", hostname: "" }

  return {
    valid: true,
    error: "",
    protocol: scheme[1].toLowerCase() + ":",
    hostname: hostname
  }
}

function priorityMeta(priority) {
  var value = Number(priority)
  if (!isFinite(value) || value < 1 || value > 5) value = 3
  if (value === 5) return { value: 5, label: "Urgent", glyph: "󰀦", urgent: true }
  if (value === 4) return { value: 4, label: "High", glyph: "󰁝", urgent: false }
  if (value === 2) return { value: 2, label: "Low", glyph: "󰁅", urgent: false }
  if (value === 1) return { value: 1, label: "Minimum", glyph: "󰂚", urgent: false }
  return { value: 3, label: "Default", glyph: "󰂚", urgent: false }
}

function relativeTime(epochSeconds, nowMs) {
  var epoch = Number(epochSeconds)
  var now = Number(nowMs)
  if (!isFinite(now)) now = Date.now()
  if (!isFinite(epoch) || epoch <= 0) return "now"
  var seconds = Math.max(0, Math.floor(now / 1000 - epoch))
  if (seconds < 60) return "now"
  if (seconds < 3600) return Math.floor(seconds / 60) + "m"
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h"
  if (seconds < 604800) return Math.floor(seconds / 86400) + "d"
  var date = new Date(epoch * 1000)
  return (date.getMonth() + 1) + "/" + date.getDate()
}

function filterRows(rows, serverId, query) {
  var source = Array.isArray(rows) ? rows : []
  var selected = String(serverId || "all")
  var needle = String(query || "").trim().toLowerCase()
  var result = source.filter(function(row) {
    if (!row || typeof row !== "object") return false
    if (selected !== "all" && String(row.serverId || "") !== selected) return false
    if (!needle) return true
    var tags = Array.isArray(row.tags) ? row.tags.join(" ") : ""
    var haystack = [row.serverLabel, row.topic, row.title, row.message, tags]
      .map(function(value) { return String(value || "").toLowerCase() }).join("\n")
    return haystack.indexOf(needle) >= 0
  })
  result.sort(function(a, b) {
    var byTime = Number(b.time || 0) - Number(a.time || 0)
    if (byTime !== 0) return byTime
    return String(b.id || "").localeCompare(String(a.id || ""))
  })
  return result
}

function tabLayout(servers, selectedServerId, maxNamedTabs) {
  var source = Array.isArray(servers) ? servers.slice() : []
  var limit = Math.max(0, Number(maxNamedTabs) || 0)
  var selected = String(selectedServerId || "all")
  var named = source.slice(0, limit)
  var selectedIndex = source.findIndex(function(server) {
    return String(server.id || "") === selected
  })
  if (selectedIndex >= limit && limit > 0) named[limit - 1] = source[selectedIndex]
  var namedIds = named.map(function(server) { return String(server.id || "") })
  var more = source.filter(function(server) {
    return namedIds.indexOf(String(server.id || "")) < 0
  })
  return {
    tabs: [{ id: "all", label: "All" }].concat(named),
    named: named,
    more: more
  }
}

function serverSummary(servers, unreadCount) {
  var source = Array.isArray(servers) ? servers : []
  var enabled = source.filter(function(server) { return server.enabled !== false })
  var connected = enabled.filter(function(server) { return server.state === "connected" }).length
  var unread = Math.max(0, Number(unreadCount) || 0)
  if (source.length === 0) return "Add a server to begin"
  return unread + " unread · " + connected + "/" + enabled.length + " servers connected"
}

function actionDisplay(action) {
  if (!action || typeof action !== "object") return ""
  var type = String(action.action || "")
  if (type === "http") {
    var method = String(action.method || "POST").toUpperCase()
    try {
      var parsed = new URL(String(action.url || ""))
      return method + " " + parsed.host
    } catch (error) {
      return method + " invalid destination"
    }
  }
  if (type === "copy") return "Copy"
  if (type === "view") return "Open"
  return ""
}

function extractLinks(text) {
  var source = String(text || "")
  var matches = source.match(/https?:\/\/[^\s<>"'`\u2026]+/g) || []
  var seen = {}
  var links = []
  for (var index = 0; index < matches.length; index++) {
    var raw = matches[index].replace(/[.,;:!?]+$/, "")
    if (!raw || seen[raw]) continue
    seen[raw] = true
    links.push({ url: raw, label: shortenUrl(raw) })
  }
  return links
}

function shortenUrl(url, maxLength) {
  var limit = Number(maxLength) || 48
  var text = String(url || "")
  try {
    var parsed = new URL(text)
    var host = parsed.host
    var path = parsed.pathname + parsed.search
    var label = host + path
    if (label.length > limit) {
      var keep = limit - host.length - 1
      label = keep > 0 ? host + path.slice(0, keep) : host
      label += "\u2026"
    }
    return label
  } catch (error) {
    return text.length > limit ? text.slice(0, limit - 1) + "\u2026" : text
  }
}

function shortenText(text) {
  return String(text || "").replace(/https?:\/\/[^\s<>"'`\u2026]+/g, function(raw) {
    var stripped = raw.replace(/[.,;:!?]+$/, "")
    if (!stripped) return raw
    return shortenUrl(stripped) + raw.slice(stripped.length)
  })
}
