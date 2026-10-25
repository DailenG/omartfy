from __future__ import annotations

import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ntfy_bridge as bridge_module
from ntfy_bridge import (
    Bridge,
    ConfigError,
    MediaError,
    SubscriptionWorker,
    atomic_json_write,
    authorization_header,
    normalize_server,
    public_server,
    stream_url,
    validate_config,
)
from mock_ntfy import MockNtfyServer, PNG


def wait_until(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RunningServer:
    def __init__(self, token="", buffered_stream=False):
        self.server = MockNtfyServer(
            ("127.0.0.1", 0), "Test", token, buffered_stream=buffered_stream
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_path = root / "config" / "omarchy" / "ntfy.json"
        self.state_dir = root / "state" / "omarchy" / "ntfy"
        self.output = io.StringIO()
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.shutdown()
        self.temp.cleanup()

    def make_bridge(self, config=None, notification_sender=None):
        if config is not None:
            atomic_json_write(self.config_path, config)
        bridge = Bridge(
            self.config_path, self.state_dir, self.output,
            notification_sender=notification_sender,
        )
        self.bridges.append(bridge)
        return bridge

    def events(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines() if line]

    def test_config_validation_secret_preservation_and_atomic_modes(self):
        server = normalize_server({
            "label": "Home",
            "baseUrl": "https://example.com/proxy/",
            "topics": ["alerts", "backups"],
            "auth": {"type": "token", "secret": "secret-value"},
        })
        self.assertRegex(server["id"], r"^[0-9a-f]{12}$")
        self.assertEqual(server["baseUrl"], "https://example.com/proxy")
        edited = normalize_server({**server, "label": "Renamed", "auth": {"type": "token", "secret": ""}}, server)
        self.assertEqual(edited["auth"]["secret"], "secret-value")
        self.assertNotIn("secret-value", json.dumps(public_server(server)))

        with self.assertRaises(ConfigError):
            validate_config({"version": 1, "servers": [server, {**server, "id": "abcdefabcdef"}]})
        with self.assertRaises(ConfigError):
            normalize_server({"label": "Bad", "baseUrl": "http://example.com", "topics": ["a"],
                              "auth": {"type": "token", "secret": "x"}})
        with self.assertRaises(ConfigError):
            normalize_server({"label": "Bad", "baseUrl": "https://user@example.com", "topics": ["a"]})
        with self.assertRaises(ConfigError):
            normalize_server({"label": "Bad", "baseUrl": "https://example.com?q=1", "topics": ["a"]})
        with self.assertRaises(ConfigError):
            normalize_server({"label": "Bad", "baseUrl": "https://example.com", "topics": ["a", "a"]})

        atomic_json_write(self.config_path, {"version": 1, "servers": [server]})
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)

    def test_basic_bearer_headers_urls_and_no_secret_emission(self):
        basic = normalize_server({"label": "Basic", "baseUrl": "https://example.com/prefix",
                                  "topics": ["one", "two"], "auth": {"type": "basic", "username": "u", "secret": "p"}})
        token = normalize_server({"label": "Token", "baseUrl": "https://example.com",
                                  "topics": ["one"], "auth": {"type": "token", "secret": "top-secret"}})
        self.assertEqual(authorization_header(basic), "Basic dTpw")
        self.assertEqual(authorization_header(token), "Bearer top-secret")
        self.assertEqual(stream_url(basic, since="all"),
                         "https://example.com/prefix/one,two/json?since=all")
        bridge = self.make_bridge({"version": 1, "servers": [basic, token]})
        bridge.emit(bridge.snapshot())
        text = self.output.getvalue()
        self.assertNotIn("top-secret", text)
        self.assertNotIn('"secret":"p"', text)

    def test_sequence_update_clear_delete_cursor_and_bootstrap_dedupe(self):
        server = normalize_server({"label": "Home", "baseUrl": "https://example.com", "topics": ["alerts"]})
        bridge = self.make_bridge({"version": 1, "servers": [server]})
        first = {"event": "message", "id": "event-one", "time": 1, "topic": "alerts",
                 "sequence_id": "sequence", "message": "one"}
        bridge.apply_event(server["id"], first)
        key = f"{server['id']}:alerts:sequence"
        self.assertEqual(bridge.server_state(server["id"])["cursor"], "event-one")
        bridge.mark_read(server["id"])
        update = {"event": "message", "id": "event-two", "time": 2, "topic": "alerts",
                  "sequence_id": "sequence", "message": "two"}
        bridge.apply_event(server["id"], update)
        row = bridge.server_state(server["id"])["notifications"][key]
        self.assertEqual(row["message"], "two")
        self.assertFalse(row["unread"])
        bridge.apply_event(server["id"], update)
        self.assertEqual(len(bridge.server_state(server["id"])["notifications"]), 1)

        bootstrap = {"event": "message", "id": "added-topic", "time": 3, "topic": "alerts", "message": "cached"}
        bridge.apply_event(server["id"], bootstrap, advance_cursor=False)
        self.assertEqual(bridge.server_state(server["id"])["cursor"], "event-two")
        bridge.apply_event(server["id"], bootstrap, advance_cursor=True)
        self.assertEqual(bridge.server_state(server["id"])["cursor"], "added-topic")

        bridge.apply_event(server["id"], {"event": "message_clear", "id": "clear-id", "topic": "alerts", "sequence_id": "sequence"})
        self.assertNotIn(key, bridge.server_state(server["id"])["notifications"])
        bridge.apply_event(server["id"], {"event": "message", "id": "third", "topic": "alerts", "sequence_id": "delete-me"})
        delete_key = f"{server['id']}:alerts:delete-me"
        bridge.apply_event(server["id"], {"event": "message_delete", "id": "delete-id", "topic": "alerts", "sequence_id": "delete-me"})
        self.assertNotIn(delete_key, bridge.server_state(server["id"])["notifications"])

    def test_read_delete_and_clear_survive_restart(self):
        server = normalize_server({"label": "Home", "baseUrl": "https://example.com", "topics": ["alerts"]})
        config = {"version": 1, "servers": [server]}
        bridge = self.make_bridge(config)
        bridge.apply_event(server["id"], {"event": "message", "id": "first", "topic": "alerts", "message": "one"})
        bridge.apply_event(server["id"], {"event": "message", "id": "second", "topic": "alerts", "message": "two"})
        first_key = f"{server['id']}:alerts:first"
        second_key = f"{server['id']}:alerts:second"
        bridge.mark_notification_read(first_key)
        state = bridge.server_state(server["id"])
        self.assertFalse(state["notifications"][first_key]["unread"])
        self.assertTrue(state["notifications"][second_key]["unread"])
        bridge.delete_notification(first_key)
        bridge.apply_event(server["id"], {"event": "message", "id": "replay", "topic": "alerts",
                                                   "sequence_id": "first", "message": "hidden"})
        self.assertNotIn(first_key, bridge.server_state(server["id"])["notifications"])
        bridge.persist_state()

        restarted = Bridge(self.config_path, self.state_dir, io.StringIO())
        self.bridges.append(restarted)
        self.assertIn("alerts:first", restarted.server_state(server["id"])["dismissed"])
        restarted.clear(server["id"])
        self.assertEqual(restarted.server_state(server["id"])["notifications"], {})

    def test_timed_and_indefinite_mute_state_survives_restart_and_expires(self):
        bridge = self.make_bridge({"version": 1, "servers": []})
        bridge.set_mute(3600)
        mute_until = bridge.current_mute_until()
        self.assertGreater(mute_until, int(time.time()))
        self.assertEqual(bridge.snapshot()["muteUntil"], mute_until)

        restarted = Bridge(self.config_path, self.state_dir, io.StringIO())
        self.bridges.append(restarted)
        self.assertEqual(restarted.current_mute_until(), mute_until)
        restarted.set_mute(-1)
        self.assertEqual(restarted.current_mute_until(), -1)
        restarted.set_mute(0)
        self.assertEqual(restarted.current_mute_until(), 0)
        restarted.state["muteUntil"] = int(time.time()) - 1
        self.assertEqual(restarted.current_mute_until(), 0)

    def test_two_simultaneous_servers_and_auth_error_state(self):
        with RunningServer() as cloud, RunningServer("right-token") as home:
            one = normalize_server({"label": "Cloud", "baseUrl": cloud.url, "topics": ["alerts"]})
            two = normalize_server({"label": "Home", "baseUrl": home.url, "topics": ["backups"],
                                    "auth": {"type": "token", "secret": "right-token"}})
            bridge = self.make_bridge({"version": 1, "servers": [one, two]})
            bridge.start_all()
            self.assertTrue(wait_until(lambda: bridge.statuses.get(one["id"], {}).get("state") == "connected"))
            self.assertTrue(wait_until(lambda: bridge.statuses.get(two["id"], {}).get("state") == "connected"))
            cloud.server.state.emit({"event": "message", "id": "cloud-event", "topic": "alerts", "message": "a"})
            home.server.state.emit({"event": "message", "id": "home-event", "topic": "backups", "message": "b"})
            self.assertTrue(wait_until(lambda: len(bridge.all_rows()) == 2))

        with RunningServer("expected") as protected:
            wrong = normalize_server({"label": "Wrong", "baseUrl": protected.url, "topics": ["alerts"],
                                      "auth": {"type": "token", "secret": "wrong"}})
            other = self.make_bridge({"version": 1, "servers": [wrong]})
            other.start_all()
            self.assertTrue(wait_until(lambda: other.statuses.get(wrong["id"], {}).get("state") == "auth-error"))
            self.assertNotIn("wrong", self.output.getvalue())

    def test_request_pacer_shares_one_budget_per_origin(self):
        now = [0.0]
        pacer = bridge_module.RequestPacer(burst=3, refill_seconds=10.0, clock=lambda: now[0])
        ntfy = ("https", "ntfy.sh", 443)
        self.assertEqual([pacer.reserve(ntfy) for _ in range(3)], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(pacer.reserve(ntfy), 10.0)
        self.assertAlmostEqual(pacer.reserve(ntfy), 20.0)
        self.assertEqual(pacer.reserve(("https", "home.example", 443)), 0.0)
        now[0] = 60.0
        self.assertEqual(pacer.reserve(ntfy), 0.0)

    def run_worker_until(self, bridge, server, responses, waits_wanted):
        """Drive one worker through scripted responses, recording URLs and backoff waits."""
        urls, waits = [], []
        worker = SubscriptionWorker(bridge, server)

        def fake_urlopen(request, timeout=None):
            urls.append(request.full_url)
            outcome = responses(len(urls))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def fake_wait(seconds=None):
            waits.append(seconds)
            if len(waits) >= waits_wanted:
                worker.stop_event.set()
            return worker.stop_event.is_set()

        bridge.urlopen = fake_urlopen
        worker.stop_event.wait = fake_wait
        worker.run()
        return urls, waits

    def test_rate_limits_back_off_hard_and_polling_returns_to_streaming(self):
        server = normalize_server({"label": "Cloud", "baseUrl": "https://ntfy.example", "topics": ["alerts"]})
        bridge = self.make_bridge({"version": 1, "servers": [server]})
        bridge.request_pacer = bridge_module.RequestPacer(burst=1000)

        def limited(_count):
            return urllib.error.HTTPError("https://ntfy.example", 429, "Too Many Requests", {}, None)
        _urls, waits = self.run_worker_until(bridge, server, limited, 6)
        self.assertGreaterEqual(min(waits), 60 * 0.85)
        self.assertGreater(waits[4], waits[0] * 8)
        self.assertLessEqual(max(waits), bridge_module.MAX_RETRY_DELAY)

        def retry_after(_count):
            return urllib.error.HTTPError("https://ntfy.example", 429, "Too Many Requests",
                                          {"Retry-After": "400"}, None)
        _urls, waits = self.run_worker_until(bridge, server, retry_after, 3)
        self.assertTrue(all(wait >= 400 for wait in waits))

        def long_retry_after(_count):
            return urllib.error.HTTPError("https://ntfy.example", 429, "Too Many Requests",
                                          {"Retry-After": "3600"}, None)
        _urls, waits = self.run_worker_until(bridge, server, long_retry_after, 1)
        self.assertEqual(waits[0], 3600)

        # Non-finite or absurd values must not overflow the wait and kill the worker.
        for header, low, high in (("inf", 60 * 0.85, bridge_module.MAX_RETRY_DELAY),
                                  ("nan", 60 * 0.85, bridge_module.MAX_RETRY_DELAY),
                                  ("-5", 60 * 0.85, bridge_module.MAX_RETRY_DELAY),
                                  ("1e12", bridge_module.MAX_RETRY_AFTER, bridge_module.MAX_RETRY_AFTER)):
            def odd_retry_after(_count, header=header):
                return urllib.error.HTTPError("https://ntfy.example", 429, "Too Many Requests",
                                              {"Retry-After": header}, None)
            _urls, waits = self.run_worker_until(bridge, server, odd_retry_after, 1)
            self.assertTrue(low <= waits[0] <= high, (header, waits[0]))

        # A stream timeout falls back to polling, and streaming is retried later
        # instead of polling forever.
        def stream_times_out(count):
            return TimeoutError("timed out") if count == 1 else io.BytesIO(b"")
        urls, _waits = self.run_worker_until(bridge, server, stream_times_out,
                                             bridge_module.POLLS_BEFORE_STREAM_RETRY + 1)
        polled = ["poll=1" in url for url in urls]
        self.assertFalse(polled[0])
        self.assertTrue(all(polled[1:bridge_module.POLLS_BEFORE_STREAM_RETRY + 1]))
        self.assertFalse(polled[bridge_module.POLLS_BEFORE_STREAM_RETRY + 1])

    def test_buffered_stream_bootstraps_and_falls_back_to_polling(self):
        old_timeout = bridge_module.STREAM_TIMEOUT_SECONDS
        old_interval = bridge_module.POLL_INTERVAL_SECONDS
        bridge_module.STREAM_TIMEOUT_SECONDS = 0.1
        bridge_module.POLL_INTERVAL_SECONDS = 0.05
        try:
            with RunningServer(buffered_stream=True) as buffered:
                buffered.server.state.emit({
                    "event": "message", "id": "cached-event", "topic": "alerts",
                    "message": "cached",
                })
                server = normalize_server({
                    "label": "Buffered", "baseUrl": buffered.url, "topics": ["alerts"],
                })
                bridge = self.make_bridge({"version": 1, "servers": [server]})
                bridge.start_all()
                self.assertTrue(wait_until(
                    lambda: bridge.server_state(server["id"]).get("cursor") == "cached-event"
                ))

                buffered.server.state.emit({
                    "event": "message", "id": "polled-event", "topic": "alerts",
                    "message": "polled",
                })
                self.assertTrue(wait_until(
                    lambda: any(row["id"] == "polled-event" for row in bridge.all_rows())
                ))
                self.assertEqual(bridge.statuses[server["id"]]["state"], "connected")
        finally:
            bridge_module.STREAM_TIMEOUT_SECONDS = old_timeout
            bridge_module.POLL_INTERVAL_SECONDS = old_interval

    def test_http_action_gating_default_post_and_correlated_results(self):
        with RunningServer() as target:
            server = normalize_server({"label": "Home", "baseUrl": target.url, "topics": ["alerts"],
                                       "enabled": False, "allowHttpActions": False})
            bridge = self.make_bridge({"version": 1, "servers": [server]})
            bridge.apply_event(server["id"], {"event": "message", "id": "action-row", "topic": "alerts",
                "actions": [{"id": "http-one", "action": "http", "label": "Ack",
                             "url": target.url + "/action-target", "body": "{\"ok\":true}"}]})
            key = f"{server['id']}:alerts:action-row"
            bridge.perform_action("blocked", key, "http-one")
            self.assertTrue(wait_until(lambda: any(event.get("requestId") == "blocked" for event in self.events())))
            blocked = next(event for event in self.events() if event.get("requestId") == "blocked")
            self.assertFalse(blocked["ok"])
            self.assertEqual(target.server.state.actions, [])

            server["allowHttpActions"] = True
            bridge.config["servers"][0]["allowHttpActions"] = True
            bridge.perform_action("allowed", key, "http-one")
            self.assertTrue(wait_until(lambda: any(event.get("requestId") == "allowed" for event in self.events())))
            allowed = next(event for event in self.events() if event.get("requestId") == "allowed")
            self.assertTrue(allowed["ok"])
            self.assertEqual(allowed["status"], 204)
            self.assertEqual(target.server.state.actions[-1]["method"], "POST")
            self.assertEqual(target.server.state.actions[-1]["body"], '{"ok":true}')
            # Cloudflare refuses Python's default User-Agent with error 1010.
            self.assertEqual(target.server.state.actions[-1]["headers"].get("User-Agent"), bridge_module.USER_AGENT)
            self.assertTrue(bridge_module.USER_AGENT.startswith("Omartfy/"))

    def test_media_allows_only_configured_origin_and_rejects_cross_origin_redirects(self):
        with RunningServer("media-token") as source, RunningServer() as target:
            server = normalize_server({"label": "Media", "baseUrl": source.url + "/proxy", "topics": ["alerts"],
                                       "enabled": False, "auth": {"type": "token", "secret": "media-token"}})
            bridge = self.make_bridge({"version": 1, "servers": [server]})
            same = bridge.download_media(server, "icon", source.url + "/icon.png")
            self.assertTrue(same.exists())
            self.assertEqual(stat.S_IMODE(same.stat().st_mode), 0o600)

            same_redirect = source.url + "/redirect?" + urllib.parse.urlencode({"to": source.url + "/icon.png"})
            redirected = bridge.download_media(server, "icon", same_redirect)
            self.assertTrue(redirected.exists())

            with self.assertRaisesRegex(MediaError, "configured server origin"):
                bridge.download_media(server, "icon", target.url + "/capture.png")
            self.assertEqual(target.server.state.actions, [])

            cross_redirect = source.url + "/redirect?" + urllib.parse.urlencode({"to": target.url + "/capture.png"})
            with self.assertRaisesRegex(MediaError, "configured server origin"):
                bridge.download_media(server, "icon", cross_redirect)
            self.assertEqual(target.server.state.actions, [])

            with self.assertRaises(MediaError):
                bridge.download_media(server, "icon", source.url + "/huge.png")
            with self.assertRaises(MediaError):
                bridge.download_media(server, "icon", source.url + "/oversized.png")
            with self.assertRaises(MediaError):
                bridge.download_media(server, "icon", source.url + "/bad-media")

            old_limit = bridge_module.MAX_MEDIA_CACHE
            bridge_module.MAX_MEDIA_CACHE = 10
            try:
                extra = bridge.media_dir / "unreferenced.bin"
                extra.write_bytes(b"x" * 20)
                bridge.enforce_media_cache()
                self.assertFalse(extra.exists())
            finally:
                bridge_module.MAX_MEDIA_CACHE = old_limit

    def test_toast_config_defaults_validation_protocol_and_persistence(self):
        defaults = validate_config({"version": 1, "servers": []})
        self.assertEqual(defaults["toasts"], {"enabled": False, "duration": "default"})

        server = normalize_server({
            "label": "Quiet", "baseUrl": "https://example.com", "topics": ["alerts"],
            "enabled": False,
        })
        self.assertTrue(server["showToasts"])
        self.assertTrue(public_server(server)["showToasts"])
        for invalid in (
            {"version": 1, "servers": [], "toasts": []},
            {"version": 1, "servers": [], "toasts": {"enabled": 1}},
            {"version": 1, "servers": [], "toasts": {"duration": "forever"}},
            {"version": 1, "servers": [{**server, "showToasts": "yes"}]},
        ):
            with self.assertRaises(ConfigError):
                validate_config(invalid)

        bridge = self.make_bridge({
            "version": 1,
            "toasts": {"enabled": True, "duration": "15-seconds"},
            "servers": [{**server, "showToasts": False}],
        })
        self.assertEqual(
            bridge.snapshot()["toastSettings"],
            {"enabled": True, "duration": "15-seconds"},
        )
        candidate = {
            "label": "Second", "baseUrl": "https://second.example.com",
            "topics": ["news"], "enabled": False, "showToasts": True,
        }
        bridge.save_server("server-save", candidate)
        saved_id = next(
            event["server"]["id"] for event in self.events()
            if event.get("requestId") == "server-save" and event["ok"]
        )
        self.assertEqual(
            json.loads(self.config_path.read_text())["toasts"],
            {"enabled": True, "duration": "15-seconds"},
        )
        bridge.delete_server("server-delete", saved_id)
        self.assertEqual(
            json.loads(self.config_path.read_text())["toasts"],
            {"enabled": True, "duration": "15-seconds"},
        )

        bridge.handle_command({
            "cmd": "save_toast_settings", "requestId": "toast-save",
            "settings": {"enabled": False, "duration": "30-seconds"},
        })
        result = next(event for event in self.events() if event.get("requestId") == "toast-save")
        self.assertEqual(result, {
            "event": "config_result", "requestId": "toast-save",
            "operation": "save_toasts", "ok": True,
            "settings": {"enabled": False, "duration": "30-seconds"},
            "server": {}, "deletedServerId": "", "error": "",
        })
        self.assertEqual(bridge.config["toasts"], result["settings"])
        bridge.save_toast_settings("toast-bad", {"enabled": True, "duration": "unknown"})
        bad = next(event for event in self.events() if event.get("requestId") == "toast-bad")
        self.assertFalse(bad["ok"])
        self.assertEqual(bridge.config["toasts"], result["settings"])
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)

    def test_batch_action_config_protocol_and_persistence(self):
        defaults = validate_config({"version": 1, "servers": []})
        self.assertEqual(defaults["batchAction"], "delete_all")
        custom = validate_config({"version": 1, "batchAction": "read_all", "servers": []})
        self.assertEqual(custom["batchAction"], "read_all")

        for invalid in (
            {"version": 1, "servers": [], "batchAction": 123},
            {"version": 1, "servers": [], "batchAction": "invalid_action"},
            {"version": 1, "servers": [], "batchAction": []},
        ):
            with self.assertRaises(ConfigError):
                validate_config(invalid)

        server = normalize_server({
            "label": "Home", "baseUrl": "https://example.com", "topics": ["alerts"],
            "enabled": False,
        })
        bridge = self.make_bridge({
            "version": 1,
            "batchAction": "read_all",
            "servers": [server],
        })
        self.assertEqual(bridge.snapshot()["batchAction"], "read_all")

        candidate = {
            "label": "Second", "baseUrl": "https://second.example.com",
            "topics": ["news"], "enabled": False,
        }
        bridge.save_server("srv-save", candidate)
        saved_id = next(
            event["server"]["id"] for event in self.events()
            if event.get("requestId") == "srv-save" and event["ok"]
        )
        self.assertEqual(
            json.loads(self.config_path.read_text())["batchAction"], "read_all"
        )
        bridge.delete_server("srv-del", saved_id)
        self.assertEqual(
            json.loads(self.config_path.read_text())["batchAction"], "read_all"
        )

        bridge.handle_command({
            "cmd": "save_batch_action", "requestId": "act-save",
            "batchAction": "delete_all",
        })
        result = next(event for event in self.events() if event.get("requestId") == "act-save")
        self.assertEqual(result, {
            "event": "config_result", "requestId": "act-save",
            "operation": "save_batch_action", "ok": True,
            "batchAction": "delete_all",
            "server": {}, "deletedServerId": "", "error": "",
        })
        self.assertEqual(bridge.config["batchAction"], "delete_all")

        bridge.save_batch_action("act-bad", "invalid_mode")
        bad = next(event for event in self.events() if event.get("requestId") == "act-bad")
        self.assertFalse(bad["ok"])
        self.assertEqual(bridge.config["batchAction"], "delete_all")

    def test_toasts_only_dispatch_for_new_live_unsuppressed_messages(self):
        sent = []
        server = normalize_server({
            "label": "Home", "baseUrl": "https://example.com", "topics": ["alerts"],
        })
        bridge = self.make_bridge({
            "version": 1,
            "toasts": {"enabled": True, "duration": "default"},
            "servers": [server],
        }, notification_sender=sent.append)
        worker = SubscriptionWorker(bridge, server)

        cached = {"event": "message", "id": "cached", "topic": "alerts", "message": "old"}
        worker._consume(io.BytesIO((json.dumps(cached) + "\n").encode()), True, False)
        self.assertEqual(sent, [])

        live = {"event": "message", "id": "live", "topic": "alerts", "message": "new"}
        worker._consume(io.BytesIO((json.dumps(live) + "\n").encode()), True, True)
        bridge.toast_executor.submit(lambda: None).result()
        self.assertEqual(len(sent), 1)
        bridge.apply_event(server["id"], live, show_toast=True)
        bridge.apply_event(server["id"], {"event": "open", "id": "open"}, show_toast=True)
        bridge.toast_executor.submit(lambda: None).result()
        self.assertEqual(len(sent), 1)

        bridge.set_mute(-1)
        bridge.apply_event(server["id"], {
            "event": "message", "id": "muted", "topic": "alerts", "message": "muted",
        }, show_toast=True)
        bridge.set_mute(0)
        bridge.server_state(server["id"])["dismissed"].append("alerts:dismissed")
        bridge.apply_event(server["id"], {
            "event": "message", "id": "dismissed-event", "sequence_id": "dismissed",
            "topic": "alerts", "message": "dismissed",
        }, show_toast=True)
        bridge.config["toasts"]["enabled"] = False
        bridge.apply_event(server["id"], {
            "event": "message", "id": "global-off", "topic": "alerts", "message": "off",
        }, show_toast=True)
        bridge.config["toasts"]["enabled"] = True
        bridge.config["servers"][0]["showToasts"] = False
        bridge.apply_event(server["id"], {
            "event": "message", "id": "server-off", "topic": "alerts", "message": "off",
        }, show_toast=True)
        bridge.config["servers"][0]["showToasts"] = True
        bridge.config["servers"][0]["enabled"] = False
        bridge.apply_event(server["id"], {
            "event": "message", "id": "disabled", "topic": "alerts", "message": "off",
        }, show_toast=True)
        bridge.toast_executor.submit(lambda: None).result()
        self.assertEqual(len(sent), 1)

    def test_native_toast_argv_is_bounded_escaped_static_and_honors_duration(self):
        sent = []
        server = normalize_server({
            "label": "Home", "baseUrl": "https://example.com", "topics": ["alerts"],
            "auth": {"type": "token", "secret": "private-token"},
        })
        bridge = self.make_bridge({
            "version": 1,
            "toasts": {"enabled": True, "duration": "default"},
            "servers": [server],
        }, notification_sender=sent.append)
        row = bridge.normalize_message(server["id"], {
            "event": "message", "id": "argv", "topic": "alerts", "priority": 1,
            "title": "--exec <b>&\" " + "x" * 300,
            "message": "Hello <script>& " + "y" * 600,
            "click": "https://click.invalid/private",
            "icon": "https://icon.invalid/private.png",
            "attachment": {"url": "https://attachment.invalid/private.png"},
            "actions": [{"id": "open", "action": "view", "url": "https://action.invalid"}],
        })
        bridge.send_native_toast(server, row)
        argv = sent.pop()
        self.assertEqual(argv[:7], [
            "omarchy-notification-send",
            "--app-name", "Omartfy",
            "--glyph", "󰂚",
            "--urgency", "low",
        ])
        self.assertEqual(argv[-6:], ["--exec", "omarchy-shell", "shell", "summon", "dailen.omartfy", "{}"])
        self.assertTrue(argv[7].startswith(" --exec "))
        self.assertIn("&lt;b&gt;&amp;&quot;", argv[7])
        self.assertIn("Hello &lt;script&gt;&amp;", argv[8])
        self.assertLessEqual(len(bridge_module.html.unescape(argv[7]).lstrip()), 160)
        self.assertLessEqual(len(bridge_module.html.unescape(argv[8])), 500)
        joined = "\0".join(argv)
        for protected in (
            "private-token", "https://click.invalid", "https://icon.invalid",
            "https://attachment.invalid", "https://action.invalid",
        ):
            self.assertNotIn(protected, joined)

        cases = {
            "8-seconds": ("normal", "8000"),
            "15-seconds": ("normal", "15000"),
            "30-seconds": ("normal", "30000"),
        }
        for duration, (urgency, timeout) in cases.items():
            bridge.config["toasts"]["duration"] = duration
            bridge.send_native_toast(server, row)
            current = sent.pop()
            self.assertEqual(current[6], urgency)
            self.assertEqual(current[9:11], ["--expire-time", timeout])

        bridge.config["toasts"]["duration"] = "until-dismissed"
        bridge.send_native_toast(server, row)
        persistent = sent.pop()
        self.assertEqual(persistent[6], "critical")
        self.assertNotIn("--expire-time", persistent)

        bridge.config["toasts"]["duration"] = "default"
        row["priority"] = 5
        bridge.send_native_toast(server, row)
        urgent = sent.pop()
        self.assertEqual(urgent[6], "critical")
        self.assertNotIn("--expire-time", urgent)

        def fail(_argv):
            raise OSError("sender unavailable")

        bridge.notification_sender = fail
        bridge.send_native_toast(server, row)
        bridge.send_native_toast(server, row)
        self.assertIn("Could not show desktop toast: sender unavailable",
                      [event.get("message") for event in self.events()])
        reason = bridge_module.Bridge.toast_failure_reason
        self.assertEqual(reason(subprocess.TimeoutExpired(["x"], 15)), "no reply within 15 seconds")
        self.assertEqual(reason(subprocess.CalledProcessError(1, ["x"], stderr=b"\nUnknown option: -z\nusage")),
                         "Unknown option: -z")
        self.assertEqual(reason(subprocess.CalledProcessError(2, ["x"], stderr=b"")), "exit status 2")
        errors = [event for event in self.events()
                  if str(event.get("message", "")).startswith("Could not show desktop toast")]
        self.assertEqual(len(errors), 1)
        bridge.notification_sender = sent.append
        bridge.send_native_toast(server, row)
        sent.pop()
        bridge.notification_sender = fail
        bridge.send_native_toast(server, row)
        errors = [event for event in self.events()
                  if str(event.get("message", "")).startswith("Could not show desktop toast")]
        bridge.notification_sender = sent.append
        malicious_row = bridge.normalize_message(server["id"], {
            "event": "message", "id": "inject", "topic": "alerts", "priority": 3,
            "title": "--icon=/etc/passwd --app-name=evil",
            "message": "--expire-time=999999 <script>alert(1)</script>",
        })
        bridge.send_native_toast(server, malicious_row)
        injected = sent.pop()
        joined = "\0".join(injected)
        self.assertIn("--icon=/etc/passwd", joined)
        self.assertIn("--app-name=evil", joined)
        self.assertIn("--expire-time=999999", joined)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", joined)
        body_arg = injected[8]
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body_arg)
        self.assertEqual(bridge_module.html.unescape(body_arg).splitlines()[1], "--expire-time=999999 <script>alert(1)</script>")
        summary_arg = injected[7]
        self.assertTrue(summary_arg.startswith(" --icon="))
        self.assertEqual(len(errors), 2)

    def test_external_media_is_opt_in_https_public_only_and_never_gets_credentials(self):
        with RunningServer("media-token") as source, RunningServer() as open_host, \
                RunningServer("media-token") as guarded_host:
            server = normalize_server({"label": "Media", "baseUrl": source.url, "topics": ["alerts"],
                                       "enabled": False, "auth": {"type": "token", "secret": "media-token"}})
            self.assertFalse(server["allowExternalMedia"])
            self.assertFalse(public_server(server)["allowExternalMedia"])
            bridge = self.make_bridge({"version": 1, "servers": [server]})
            with self.assertRaisesRegex(MediaError, "configured server origin"):
                bridge.download_media(server, "icon", open_host.url + "/icon.png")

            server = normalize_server({**server, "allowExternalMedia": True})
            self.assertTrue(public_server(server)["allowExternalMedia"])
            # Same-origin media keeps working even though the server is local.
            self.assertTrue(bridge.download_media(server, "icon", source.url + "/icon.png").exists())
            with self.assertRaisesRegex(MediaError, "HTTPS"):
                bridge.download_media(server, "icon", open_host.url + "/icon.png")
            for private in ("https://127.0.0.1/a.png", "https://localhost/a.png", "https://10.1.2.3/a.png",
                            "https://192.168.1.5/a.png", "https://100.64.0.1/a.png", "https://[::1]/a.png",
                            "https://169.254.169.254/a.png"):
                with self.assertRaisesRegex(MediaError, "public addresses"):
                    bridge.download_media(server, "icon", private)
            to_local = source.url + "/redirect?" + urllib.parse.urlencode({"to": "https://127.0.0.1/a.png"})
            with self.assertRaisesRegex(MediaError, "public addresses"):
                bridge.download_media(server, "icon", to_local)
            self.assertTrue(bridge_module.resolves_to_public_addresses("8.8.8.8", 443))
            with unittest.mock.patch.object(bridge_module.socket, "getaddrinfo",
                                            side_effect=OSError("no such host")):
                self.assertFalse(bridge_module.resolves_to_public_addresses("invalid.invalid", 443))

            # With the host policy out of the way, the server's token still never
            # reaches another host: one that demands it gets no header and refuses.
            # The pinned address is what gets connected to, not a fresh DNS answer:
            # "media.invalid" never resolves, yet the fetch reaches the mock host.
            open_port = urllib.parse.urlsplit(open_host.url).port
            guarded_port = urllib.parse.urlsplit(guarded_host.url).port
            with unittest.mock.patch.object(bridge_module, "validate_media_url",
                                            lambda _server, value: bridge_module.validate_web_url(value)), \
                    unittest.mock.patch.object(bridge_module, "resolve_public_address",
                                               lambda _host, _port, _timeout: "127.0.0.1"), \
                    unittest.mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"}):
                self.assertTrue(bridge.download_media(
                    server, "icon", f"http://media.invalid:{open_port}/icon.png").exists())
                with self.assertRaisesRegex(MediaError, "returned 401"):
                    bridge.download_media(server, "icon", f"http://media.invalid:{guarded_port}/icon.png")

            def slow_lookup(*_args, **_kwargs):
                time.sleep(2)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
            with unittest.mock.patch.object(bridge_module.socket, "getaddrinfo", slow_lookup):
                started = time.monotonic()
                with self.assertRaisesRegex(MediaError, "lookup timed out"):
                    bridge_module.resolve_public_address("slow.example", 443, 0.2)
                self.assertLess(time.monotonic() - started, 1.0)
            mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
            with unittest.mock.patch.object(bridge_module.socket, "getaddrinfo", return_value=mixed):
                with self.assertRaisesRegex(MediaError, "public addresses"):
                    bridge_module.resolve_public_address("mixed.example", 443, 1.0)

    def test_native_toast_shows_same_origin_image_attachment(self):
        with RunningServer("media-token") as source:
            sent = []
            server = normalize_server({"label": "Media", "baseUrl": source.url + "/proxy", "topics": ["alerts"],
                                       "enabled": False, "auth": {"type": "token", "secret": "media-token"}})
            bridge = self.make_bridge({
                "version": 1,
                "toasts": {"enabled": True, "duration": "default"},
                "servers": [server],
            }, notification_sender=sent.append)
            bridge.config["servers"][0]["enabled"] = True
            row = bridge.normalize_message(server["id"], {
                "event": "message", "id": "picture", "topic": "alerts", "title": "Snapshot",
                "attachment": {"name": "icon.png", "type": "image/png", "url": source.url + "/icon.png"},
            })
            bridge.send_native_toast(server, row)
            argv = sent.pop()
            image = argv[argv.index("--image") + 1]
            self.assertLess(argv.index("--image"), argv.index("--exec"))
            self.assertTrue(Path(image).exists())
            self.assertTrue(Path(image).is_relative_to(bridge.media_dir))

            for attachment in (
                {"name": "notes.pdf", "type": "application/pdf", "url": source.url + "/icon.png"},
                {"name": "old.png", "type": "image/png", "url": source.url + "/icon.png", "expires": 1},
                {"name": "bad.png", "type": "image/png", "url": source.url + "/bad-media"},
            ):
                row["attachment"] = attachment
                bridge.send_native_toast(server, row)
                self.assertNotIn("--image", sent.pop())

            # A cache already full of referenced media evicts the new, unreferenced
            # toast image straight away; the toast must not point at a missing file.
            row["attachment"] = {"name": "icon.png", "type": "image/png", "url": source.url + "/icon.png"}
            bridge.enforce_media_cache = lambda: [path.unlink() for path in bridge.media_dir.iterdir()]
            bridge.send_native_toast(server, row)
            self.assertNotIn("--image", sent.pop())

    def test_save_delete_results_and_malformed_config_protection(self):
        bridge = self.make_bridge({"version": 1, "servers": []})
        candidate = {"label": "Offline", "baseUrl": "https://example.com", "topics": ["alerts"], "enabled": False}
        bridge.save_server("save-ok", candidate)
        saved = next(event for event in self.events() if event.get("requestId") == "save-ok")
        self.assertTrue(saved["ok"])
        server_id = saved["server"]["id"]
        bridge.delete_server("delete-ok", server_id)
        deleted = next(event for event in self.events() if event.get("requestId") == "delete-ok")
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["deletedServerId"], server_id)
        bridge.delete_server("delete-bad", server_id)
        self.assertFalse(next(event for event in self.events() if event.get("requestId") == "delete-bad")["ok"])

        malformed_path = Path(self.temp.name) / "malformed.json"
        malformed_path.write_text("{bad", encoding="utf-8")
        malformed = Bridge(malformed_path, Path(self.temp.name) / "malformed-state", io.StringIO())
        self.bridges.append(malformed)
        malformed.save_server("no-overwrite", candidate)
        malformed.save_toast_settings("no-toast-overwrite", {
            "enabled": True, "duration": "8-seconds",
        })
        malformed.save_batch_action("no-batch-overwrite", "read_all")
        self.assertEqual(malformed_path.read_text(encoding="utf-8"), "{bad")

    def test_json_lines_subprocess_correlates_commands_and_exits_zero(self):
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(Path(self.temp.name) / "sub-config")
        env["XDG_STATE_HOME"] = str(Path(self.temp.name) / "sub-state")
        command = json.dumps({"cmd": "save_server", "requestId": "sub-save", "server": {
            "label": "Offline", "baseUrl": "https://example.com", "topics": ["alerts"], "enabled": False
        }}) + "\n"
        process = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "ntfy_bridge.py")],
                                 input=command, text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        events = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(events[0]["event"], "snapshot")
        result = next(event for event in events if event.get("requestId") == "sub-save")
        self.assertEqual(result["event"], "config_result")
        self.assertTrue(result["ok"])
        self.assertTrue(any(event["event"] == "snapshot" and event["servers"] for event in events[1:]))


if __name__ == "__main__":
    unittest.main()
