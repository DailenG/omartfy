const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8")
  .replace(/^\.pragma library\s*/m, "");
const model = { URL, Date, JSON, Array, Number, String, Math, isFinite };
vm.createContext(model);
vm.runInContext(source, model);

test("parseLine isolates malformed input", () => {
  assert.equal(model.parseLine("not-json"), null);
  assert.deepEqual(JSON.parse(JSON.stringify(model.parseLine('{"event":"snapshot"}'))), { event: "snapshot" });
});

test("parseBaseUrl accepts ordinary hosts without relying on URL object properties", () => {
  const parsed = model.parseBaseUrl("https://ntfy.cloud.widedata.host");
  assert.equal(parsed.valid, true);
  assert.equal(parsed.protocol, "https:");
  assert.equal(parsed.hostname, "ntfy.cloud.widedata.host");
  assert.equal(model.parseBaseUrl("http://127.0.0.1:8099/ntfy").hostname, "127.0.0.1");
});

test("parseBaseUrl rejects credentials, query, fragment, and relative URLs", () => {
  assert.equal(model.parseBaseUrl("https://user:secret@example.com").error, "forbidden");
  assert.equal(model.parseBaseUrl("https://example.com?token=secret").error, "forbidden");
  assert.equal(model.parseBaseUrl("https://example.com/#inbox").error, "forbidden");
  assert.equal(model.parseBaseUrl("example.com").error, "absolute");
});

test("priorityMeta defaults and marks urgent", () => {
  assert.equal(model.priorityMeta(undefined).value, 3);
  assert.equal(model.priorityMeta(9).value, 3);
  assert.equal(model.priorityMeta(5).urgent, true);
  assert.equal(model.priorityMeta(4).label, "High");
});

test("relativeTime honors minute hour day and old-date boundaries", () => {
  const now = Date.UTC(2026, 7, 19, 12, 0, 0);
  assert.equal(model.relativeTime(now / 1000 - 59, now), "now");
  assert.equal(model.relativeTime(now / 1000 - 60, now), "1m");
  assert.equal(model.relativeTime(now / 1000 - 3600, now), "1h");
  assert.equal(model.relativeTime(now / 1000 - 86400, now), "1d");
  assert.match(model.relativeTime(now / 1000 - 604800, now), /^\d+\/\d+$/);
});

test("filterRows searches all promised fields and sorts newest first", () => {
  const rows = [
    { serverId: "a", serverLabel: "Cloud", topic: "alerts", title: "Camera", message: "Front door", tags: ["warning"], time: 10, id: "a", unread: true },
    { serverId: "b", serverLabel: "Home", topic: "backups", title: "Done", message: "Archive ready", tags: ["storage"], time: 20, id: "b", unread: false },
  ];
  assert.deepEqual(Array.from(model.filterRows(rows, "all", ""), row => row.id), ["b", "a"]);
  assert.deepEqual(Array.from(model.filterRows(rows, "all", "", true), row => row.id), ["a"]);
  assert.deepEqual(Array.from(model.filterRows(rows, "all", "CLOUD"), row => row.id), ["a"]);
  assert.deepEqual(Array.from(model.filterRows(rows, "all", "ALERTS"), row => row.id), ["a"]);
  assert.deepEqual(Array.from(model.filterRows(rows, "all", "front"), row => row.id), ["a"]);
  assert.deepEqual(Array.from(model.filterRows(rows, "all", "WARNING"), row => row.id), ["a"]);
  assert.deepEqual(Array.from(model.filterRows(rows, "b", ""), row => row.id), ["b"]);
});

test("tabLayout keeps All, three names, active overflow, and deterministic More", () => {
  const servers = ["A", "B", "C", "D", "E"].map((label, index) => ({ id: String(index + 1), label }));
  let layout = model.tabLayout(servers, "all", 3);
  assert.deepEqual(Array.from(layout.tabs, tab => tab.label), ["All", "A", "B", "C"]);
  assert.deepEqual(Array.from(layout.more, tab => tab.label), ["D", "E"]);
  layout = model.tabLayout(servers, "5", 3);
  assert.deepEqual(Array.from(layout.tabs, tab => tab.label), ["All", "A", "B", "E"]);
  assert.deepEqual(Array.from(layout.more, tab => tab.label), ["C", "D"]);
});

test("serverSummary counts enabled connected servers", () => {
  assert.equal(model.serverSummary([], 0), "Add a server to begin");
  const servers = [
    { enabled: true, state: "connected" },
    { enabled: true, state: "backoff" },
    { enabled: false, state: "disabled" },
  ];
  assert.equal(model.serverSummary(servers, 7), "7 unread · 1/2 servers connected");
});

test("actionDisplay exposes method and host, not headers", () => {
  assert.equal(model.actionDisplay({ action: "http", method: "", url: "https://example.com:8443/do" }), "POST example.com:8443");
  assert.equal(model.actionDisplay({ action: "copy" }), "Copy");
});

test("extractLinks finds urls, trims punctuation, and dedupes", () => {
  const links = model.extractLinks("See https://a.com/x. and https://a.com/x. plus http://b.org/y");
  assert.deepEqual(Array.from(links, link => link.url), ["https://a.com/x", "http://b.org/y"]);
  assert.equal(model.extractLinks("no links here").length, 0);
});

test("shortenUrl keeps host and elides long paths", () => {
  assert.equal(model.shortenUrl("https://github.com/a/b"), "github.com/a/b");
  assert.equal(model.shortenUrl("https://github.com/a/very/long/path", 20), "github.com/a/very/l\u2026");
  assert.equal(model.shortenUrl("not a url", 10), "not a url");
});

test("shortenText replaces urls in prose and preserves trailing punctuation", () => {
  assert.equal(model.shortenText("See https://github.com/a/b now."), "See github.com/a/b now.");
  assert.equal(model.shortenText("plain text"), "plain text");
  assert.equal(
    model.shortenText("Go to https://github.com/a/very/long/path/with/many/more/segments"),
    "Go to github.com/a/very/long/path/with/many/more/segm\u2026"
  );
});

test("plainText strips markup, control chars, bidi overrides, and caps length", () => {
  assert.equal(model.plainText("<img src=x>&\u202eAlert\nNow"), "img src=x Alert Now");
  assert.equal(model.plainText("abcdef", 4), "abc\u2026");
});
