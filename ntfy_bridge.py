#!/usr/bin/env python3
"""JSON-lines bridge between Omarchy's QML service and multiple ntfy servers."""

from __future__ import annotations

import base64
import hashlib
import html
import http.client
import ipaddress
import json
import math
import os
import random
import re
import secrets
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO, Callable, TextIO

TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_ROWS = 100
MAX_RECENT_IDS = 500
MAX_DISMISSED = 200
MAX_MEDIA_FILE = 5 * 1024 * 1024
MAX_MEDIA_CACHE = 50 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
# ntfy sends a keepalive every 45 seconds by default, so a quiet stream is
# only dead once it has been silent for well over that.
def plugin_version() -> str:
    try:
        manifest = json.loads((Path(__file__).resolve().parent / "manifest.json").read_text(encoding="utf-8"))
        return str(manifest.get("version") or "dev")
    except (OSError, ValueError, AttributeError):
        return "dev"


# Sent on every request. Python's default "Python-urllib/3.x" is refused by
# common bot filters (Cloudflare answers it with error 1010) before the
# request reaches the ntfy server or an HTTP action's target.
USER_AGENT = f"Omartfy/{plugin_version()} (+https://github.com/DailenG/omartfy)"
STREAM_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 60
# A server that fell back to polling retries streaming after this many polls.
POLLS_BEFORE_STREAM_RETRY = 10
# Subscription requests share one budget per server origin, well under
# ntfy.sh's default visitor limit (a burst of 60, then one every 5 seconds),
# however many profiles point at that origin.
REQUEST_BURST = 20
REQUEST_REFILL_SECONDS = 10.0
RETRY_DELAYS = [2, 4, 8, 16, 32, 60, 120, 300]
RATE_LIMIT_MIN_DELAY = 60.0
MAX_RETRY_DELAY = 600.0
MAX_RETRY_AFTER = 86400.0
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
FORBIDDEN_ACTION_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}
BATCH_ACTIONS = {"delete_all", "read_all"}
TOAST_DURATIONS = {"default", "8-seconds", "15-seconds", "30-seconds", "until-dismissed"}
QML_ROW_KEYS = (
    "notificationKey", "serverId", "serverLabel", "id", "effectiveSequence",
    "time", "expires", "topic", "message", "title", "tags", "priority",
    "unread", "click", "actions", "attachment", "icon", "contentType",
    "iconPath", "attachmentPath",
)


class ConfigError(ValueError):
    pass


class MediaError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: BinaryIO, code: int,
                         msg: str, headers: Any, newurl: str) -> None:
        return None


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "omarchy" / "ntfy.json"


def default_state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "omarchy" / "ntfy"


def atomic_json_write(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def is_loopback_host(hostname: str | None) -> bool:
    host = (hostname or "").rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.endswith("/"):
        url = url[:-1]
    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise ConfigError("Base URL has an invalid port") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ConfigError("Base URL must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("Base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("Base URL must not contain a query or fragment")
    return url


def normalize_toast_settings(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ConfigError("Toast settings must be an object")
    enabled = candidate.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("Toast enabled setting must be a boolean")
    duration = candidate.get("duration", "default")
    if not isinstance(duration, str) or duration not in TOAST_DURATIONS:
        raise ConfigError("Unknown toast duration")
    return {"enabled": enabled, "duration": duration}


def normalize_batch_action(candidate: Any) -> str:
    if not isinstance(candidate, str) or candidate not in BATCH_ACTIONS:
        raise ConfigError("Batch action must be delete_all or read_all")
    return candidate


def normalize_server(candidate: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ConfigError("Server profile must be an object")
    server_id = str(candidate.get("id") or "")
    if not server_id:
        server_id = secrets.token_hex(6)
    if not ID_RE.fullmatch(server_id):
        raise ConfigError("Server id must be 12 lowercase hexadecimal characters")

    label = str(candidate.get("label") or "").strip()
    if not label:
        raise ConfigError("Label is required")
    base_url = normalize_base_url(candidate.get("baseUrl"))

    raw_topics = candidate.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ConfigError("At least one topic is required")
    topics: list[str] = []
    seen_topics: set[str] = set()
    for raw_topic in raw_topics:
        topic = str(raw_topic)
        if not TOPIC_RE.fullmatch(topic):
            raise ConfigError(f"Invalid topic: {topic[:64]}")
        if topic in seen_topics:
            raise ConfigError(f"Duplicate topic: {topic}")
        seen_topics.add(topic)
        topics.append(topic)

    raw_auth = candidate.get("auth") or {}
    if not isinstance(raw_auth, dict):
        raise ConfigError("Authentication must be an object")
    auth_type = str(raw_auth.get("type") or "none")
    if auth_type not in {"none", "token", "basic"}:
        raise ConfigError("Authentication type must be none, token, or basic")
    username = str(raw_auth.get("username") or "")
    secret = str(raw_auth.get("secret") or "")
    old_auth = (existing or {}).get("auth") or {}
    if auth_type == "none":
        username = ""
        secret = ""
    else:
        if auth_type == "basic" and not username:
            raise ConfigError("A username is required for Basic authentication")
        if not secret:
            if existing and str(old_auth.get("type") or "none") == auth_type:
                secret = str(old_auth.get("secret") or "")
            else:
                raise ConfigError("A new secret is required when changing authentication")
        if not secret:
            raise ConfigError("Authentication secret is required")

    allow_insecure = bool(candidate.get("allowInsecureHttp", False))
    parsed = urllib.parse.urlsplit(base_url)
    if auth_type != "none" and parsed.scheme == "http" and not is_loopback_host(parsed.hostname) and not allow_insecure:
        raise ConfigError("Credentials over non-loopback HTTP require explicit acknowledgment")

    show_toasts = candidate.get("showToasts", True)
    if not isinstance(show_toasts, bool):
        raise ConfigError("Show toasts setting must be a boolean")

    return {
        "id": server_id,
        "label": label,
        "baseUrl": base_url,
        "topics": topics,
        "enabled": bool(candidate.get("enabled", True)),
        "showToasts": show_toasts,
        "allowHttpActions": bool(candidate.get("allowHttpActions", False)),
        "allowExternalMedia": bool(candidate.get("allowExternalMedia", False)),
        "allowInsecureHttp": allow_insecure,
        "auth": {"type": auth_type, "username": username, "secret": secret},
    }


def validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("servers"), list):
        raise ConfigError("Configuration must have version 1 and a servers array")
    toast_settings = normalize_toast_settings(raw["toasts"]) if "toasts" in raw else {
        "enabled": False, "duration": "default"
    }
    batch_action = normalize_batch_action(raw.get("batchAction", "delete_all"))
    servers: list[dict[str, Any]] = []
    ids: set[str] = set()
    labels: set[str] = set()
    for candidate in raw["servers"]:
        server = normalize_server(candidate)
        folded = server["label"].casefold()
        if folded in labels:
            raise ConfigError("Server labels must be unique")
        if server["id"] in ids:
            raise ConfigError("Server ids must be unique")
        labels.add(folded)
        ids.add(server["id"])
        servers.append(server)
    return {
        "version": 1,
        "toasts": toast_settings,
        "batchAction": batch_action,
        "servers": servers,
    }
def public_server(server: dict[str, Any], state: str = "disabled", error: str = "") -> dict[str, Any]:
    auth = server.get("auth") or {}
    return {
        "id": server["id"],
        "label": server["label"],
        "baseUrl": server["baseUrl"],
        "topics": list(server["topics"]),
        "enabled": bool(server["enabled"]),
        "allowHttpActions": bool(server["allowHttpActions"]),
        "allowExternalMedia": bool(server.get("allowExternalMedia", False)),
        "showToasts": bool(server["showToasts"]),
        "allowInsecureHttp": bool(server["allowInsecureHttp"]),
        "auth": {
            "type": str(auth.get("type") or "none"),
            "username": str(auth.get("username") or ""),
            "hasSecret": bool(auth.get("secret")),
        },
        "state": state,
        "error": error,
    }


def authorization_header(server: dict[str, Any]) -> str:
    auth = server.get("auth") or {}
    auth_type = auth.get("type")
    if auth_type == "token":
        return "Bearer " + str(auth.get("secret") or "")
    if auth_type == "basic":
        value = f"{auth.get('username', '')}:{auth.get('secret', '')}".encode("utf-8")
        return "Basic " + base64.b64encode(value).decode("ascii")
    return ""


def stream_url(server: dict[str, Any], topics: list[str] | None = None,
               since: str = "all", poll: bool = False) -> str:
    selected = topics if topics is not None else server["topics"]
    joined = ",".join(urllib.parse.quote(topic, safe="-_A-Za-z0-9") for topic in selected)
    query: list[tuple[str, str]] = [("since", since or "all")]
    if poll:
        query.insert(0, ("poll", "1"))
    return f"{server['baseUrl']}/{joined}/json?{urllib.parse.urlencode(query)}"


def effective_port(parsed: urllib.parse.SplitResult) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def origin_tuple(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), effective_port(parsed)


def validate_web_url(value: Any) -> str:
    url = str(value or "")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("Only HTTP and HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing credentials are not allowed")
    return url


def resolve_public_address(hostname: str, port: int, timeout: float) -> str:
    """Resolve within the timeout and return an address to pin, if every result is public."""
    outcome: dict[str, Any] = {}

    def lookup() -> None:
        try:
            outcome["infos"] = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as error:
            outcome["error"] = error

    # getaddrinfo takes no timeout, so the lookup runs on a daemon thread that
    # is abandoned if the system resolver outlasts the deadline.
    resolver = threading.Thread(target=lookup, name="ntfy-resolve", daemon=True)
    resolver.start()
    resolver.join(max(0.0, timeout))
    if resolver.is_alive():
        raise MediaError("External media lookup timed out")
    addresses = [info[4][0].split("%", 1)[0] for info in outcome.get("infos") or []]
    parsed = {ipaddress.ip_address(address) for address in addresses}
    if not parsed or not all(address.is_global and not address.is_multicast for address in parsed):
        raise MediaError("External media must resolve to public addresses")
    return addresses[0]


def resolves_to_public_addresses(hostname: str, port: int, timeout: float = 8.0) -> bool:
    try:
        resolve_public_address(hostname, port, timeout)
    except MediaError:
        return False
    return True


class PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to an address checked in advance, never a fresh DNS answer."""

    def __init__(self, host: str, address: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self.pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.pinned_address, self.port), self.timeout, self.source_address)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to a pre-checked address, verifying the certificate for the hostname."""

    def __init__(self, host: str, address: str, **kwargs: Any) -> None:
        self.pinned_context = ssl.create_default_context()
        super().__init__(host, context=self.pinned_context, **kwargs)
        self.pinned_address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.pinned_address, self.port), self.timeout, self.source_address)
        self.sock = self.pinned_context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, address: str) -> None:
        super().__init__()
        self.pinned_address = address

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(lambda host, **kwargs: PinnedHTTPConnection(host, self.pinned_address, **kwargs), req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, address: str) -> None:
        super().__init__()
        self.pinned_address = address

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(lambda host, **kwargs: PinnedHTTPSConnection(host, self.pinned_address, **kwargs), req)


def pinned_media_opener(address: str) -> urllib.request.OpenerDirector:
    # An empty ProxyHandler keeps ambient proxy settings from carrying the
    # request somewhere other than the pinned address.
    return urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}),
                                       PinnedHTTPHandler(address), PinnedHTTPSHandler(address))


def validate_media_url(server: dict[str, Any], value: Any) -> str:
    url = validate_web_url(value)
    if origin_tuple(url) == origin_tuple(server["baseUrl"]):
        return url
    if not server.get("allowExternalMedia", False):
        raise MediaError("Media URL must use the configured server origin")
    # The external-media opt-in covers other public hosts, never the local
    # network, and never plaintext. Redirects come back through here too;
    # download_media resolves and pins a public address for each hop.
    if origin_tuple(url)[0] != "https":
        raise MediaError("External media must use HTTPS")
    return url


def clean_attachment(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("name", "url", "type"):
        if key in raw:
            result[key] = str(raw.get(key) or "")
    for key in ("size", "expires"):
        if key in raw:
            try:
                result[key] = int(raw.get(key) or 0)
            except (TypeError, ValueError):
                result[key] = 0
    return result


def normalize_actions(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    public: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return public, protected
    for index, candidate in enumerate(raw[:3]):
        if not isinstance(candidate, dict):
            continue
        action_type = str(candidate.get("action") or "")
        if action_type not in {"view", "http", "copy"}:
            continue
        action_id = str(candidate.get("id") or f"action-{index + 1}")
        method = str(candidate.get("method") or ("POST" if action_type == "http" else "")).upper()
        item = {
            "id": action_id,
            "action": action_type,
            "label": str(candidate.get("label") or action_type.title()),
            "clear": bool(candidate.get("clear", False)),
            "url": str(candidate.get("url") or ""),
            "method": method,
        }
        public.append(dict(item))
        private = dict(item)
        if action_type == "copy":
            private["value"] = str(candidate.get("value") or "")
        if action_type == "http":
            headers = candidate.get("headers")
            private["headers"] = dict(headers) if isinstance(headers, dict) else {}
            body = candidate.get("body")
            private["body"] = "" if body is None else str(body)
        protected.append(private)
    return public, protected


def qml_notification(row: dict[str, Any]) -> dict[str, Any]:
    actions = row.get("actions") if isinstance(row.get("actions"), list) else []
    value = {key: row.get(key) for key in QML_ROW_KEYS}
    value["actions"] = [
        {field: action.get(field) for field in ("id", "action", "label", "clear", "url", "method")}
        for action in actions if isinstance(action, dict)
    ]
    value["tags"] = list(row.get("tags") or [])
    value["attachment"] = dict(row.get("attachment") or {})
    return value


def bounded_text(value: Any, limit: int = 256) -> str:
    text = " ".join(str(value or "").splitlines()).strip()
    return text[:limit]


def image_info(data: bytes) -> tuple[str, int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if data[:3] == b"\xff\xd8\xff":
        offset = 2
        while offset + 9 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            length = struct.unpack(">H", data[offset:offset + 2])[0]
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                return "jpg", width, height
            offset += length
        raise MediaError("Invalid JPEG dimensions")
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "gif", width, height
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return "webp", width, height
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            return "webp", width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        raise MediaError("Invalid WebP dimensions")
    raise MediaError("Unsupported image format")


class RequestPacer:
    """Token bucket per server origin, shared by every profile on that origin."""

    def __init__(self, burst: int = REQUEST_BURST, refill_seconds: float = REQUEST_REFILL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.burst = float(burst)
        self.refill_seconds = float(refill_seconds)
        self.clock = clock
        self.lock = threading.Lock()
        self.buckets: dict[tuple[str, str, int], tuple[float, float]] = {}

    def reserve(self, origin: tuple[str, str, int]) -> float:
        """Take one request slot and return how long to wait before using it."""
        with self.lock:
            now = self.clock()
            tokens, last = self.buckets.get(origin, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) / self.refill_seconds) - 1.0
            self.buckets[origin] = (tokens, now)
            return 0.0 if tokens >= 0 else -tokens * self.refill_seconds


class SubscriptionWorker(threading.Thread):
    def __init__(self, bridge: "Bridge", server: dict[str, Any],
                 bootstrap_topics: list[str] | None = None,
                 cursor_override: str | None = None,
                 bootstrap_advance_cursor: bool = False) -> None:
        super().__init__(name=f"ntfy-{server['id']}", daemon=True)
        self.bridge = bridge
        self.server = server
        self.bootstrap_topics = list(bootstrap_topics or [])
        self.cursor_override = cursor_override
        self.bootstrap_advance_cursor = bootstrap_advance_cursor
        self.stop_event = threading.Event()
        self.response: Any = None
        self.response_lock = threading.Lock()

    def stop(self) -> None:
        self.stop_event.set()
        with self.response_lock:
            response = self.response
        if response is not None:
            # close() waits for the worker's in-flight readline, which can sit for
            # the whole stream timeout; shutting the socket down wakes it now.
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                response.close()
            except Exception:
                pass

    def _open(self, topics: list[str], since: str, poll: bool) -> Any:
        wait = self.bridge.request_pacer.reserve(origin_tuple(self.server["baseUrl"]))
        if wait and (self.stop_event.wait(wait) or self.bridge.stop_event.is_set()):
            raise InterruptedError("Subscription stopped")
        request = urllib.request.Request(stream_url(self.server, topics, since, poll), method="GET",
                                         headers={"User-Agent": USER_AGENT})
        authorization = authorization_header(self.server)
        if authorization:
            request.add_header("Authorization", authorization)
        return self.bridge.urlopen(request, timeout=10 if poll else STREAM_TIMEOUT_SECONDS)

    def _consume(self, response: Any, advance_cursor: bool, show_toast: bool) -> None:
        while not self.stop_event.is_set() and not self.bridge.stop_event.is_set():
            line = response.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.bridge.emit({"event": "error", "fatal": False, "message": "Ignored malformed ntfy stream data"})
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "open":
                self.bridge.set_server_status(self.server["id"], "connected", "")
            self.bridge.apply_event(
                self.server["id"], event, advance_cursor=advance_cursor, show_toast=show_toast
            )

    def run(self) -> None:
        server_id = self.server["id"]
        if self.bootstrap_topics and not self.stop_event.is_set():
            try:
                with self._open(self.bootstrap_topics, "all", True) as response:
                    self.bridge.set_server_status(server_id, "connected", "")
                    self._consume(
                        response, advance_cursor=self.bootstrap_advance_cursor, show_toast=False
                    )
            except urllib.error.HTTPError as error:
                error.close()
                if error.code in {401, 403}:
                    self.bridge.set_server_status(server_id, "auth-error", "Authentication failed")
                    return
                self.bridge.emit({"event": "error", "fatal": False,
                                  "message": f"Could not load topics: HTTP {error.code}"})
            except InterruptedError:
                return
            except Exception as error:
                self.bridge.emit({"event": "error", "fatal": False,
                                  "message": self.bridge.safe_error(error, "Could not load topics")})

        attempt = 0
        rate_limited = 0
        poll_mode = False
        polls = 0
        retry_after = 0.0
        while not self.stop_event.is_set() and not self.bridge.stop_event.is_set():
            if not poll_mode:
                self.bridge.set_server_status(server_id, "connecting", "")
            with self.bridge.state_lock:
                cursor = self.cursor_override
                if cursor is None:
                    cursor = str(self.bridge.server_state(server_id).get("cursor") or "all")
                self.cursor_override = None
            try:
                response = self._open(self.server["topics"], cursor or "all", poll_mode)
                with self.response_lock:
                    self.response = response
                attempt = 0
                rate_limited = 0
                self.bridge.set_server_status(server_id, "connected", "")
                try:
                    self._consume(response, advance_cursor=True, show_toast=True)
                finally:
                    response.close()
                    with self.response_lock:
                        self.response = None
                if self.stop_event.is_set() or self.bridge.stop_event.is_set():
                    return
                if poll_mode:
                    polls += 1
                    if polls >= POLLS_BEFORE_STREAM_RETRY:
                        poll_mode, polls = False, 0
                    self.stop_event.wait(POLL_INTERVAL_SECONDS)
                    continue
                raise EOFError("ntfy stream closed")
            except urllib.error.HTTPError as error:
                with self.response_lock:
                    self.response = None
                error.close()
                if error.code in {401, 403}:
                    self.bridge.set_server_status(server_id, "auth-error", "Authentication failed")
                    return
                if error.code == 429:
                    # Hammering a rate limit gets the address blocked outright, so
                    # back off hard and keep doubling while it persists.
                    try:
                        retry_after = float(error.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    # inf, NaN or an absurd value would overflow the wait below
                    # and kill this worker; a day is the longest honoured.
                    if not math.isfinite(retry_after) or retry_after < 0:
                        retry_after = 0.0
                    retry_after = min(retry_after, MAX_RETRY_AFTER)
                    delay = max(retry_after, RATE_LIMIT_MIN_DELAY * 2 ** min(rate_limited, 4))
                    rate_limited += 1
                else:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    attempt += 1
                message = f"HTTP {error.code}"
            except TimeoutError as error:
                with self.response_lock:
                    self.response = None
                if self.stop_event.is_set() or self.bridge.stop_event.is_set():
                    return
                if not poll_mode:
                    poll_mode, polls = True, 0
                    continue
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                attempt += 1
                message = self.bridge.safe_error(error, "Polling timed out")
            except Exception as error:
                with self.response_lock:
                    self.response = None
                if self.stop_event.is_set() or self.bridge.stop_event.is_set():
                    return
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                attempt += 1
                message = self.bridge.safe_error(error, "Connection lost")
            # A server-requested Retry-After is a floor that neither jitter nor
            # the cap may undercut.
            jittered = max(retry_after, min(MAX_RETRY_DELAY, delay * random.uniform(0.85, 1.15)))
            retry_after = 0.0
            self.bridge.set_server_status(server_id, "backoff", message)
            self.stop_event.wait(jittered)


class Bridge:
    def __init__(self, config_path: Path | None = None, state_dir: Path | None = None,
                 output: TextIO | None = None, urlopen: Any = None,
                 notification_sender: Callable[[list[str]], None] | None = None) -> None:
        self.config_path = Path(config_path or default_config_path())
        self.state_dir = Path(state_dir or default_state_dir())
        self.state_path = self.state_dir / "state.json"
        self.media_dir = self.state_dir / "media"
        self.output = output or sys.stdout
        self.urlopen = urlopen or urllib.request.urlopen
        self.notification_sender = notification_sender or self._run_notification_sender
        self.stdout_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.workers_lock = threading.Lock()
        self.toast_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ntfy-task")
        # One worker keeps toasts in arrival order while their image downloads
        # stay off the subscription streams.
        self.toast_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ntfy-toast")
        self.request_pacer = RequestPacer()
        self.workers: dict[str, SubscriptionWorker] = {}
        self.statuses: dict[str, dict[str, str]] = {}
        self.inflight_actions: set[tuple[str, str]] = set()
        self.toast_error_active = False
        self.config_malformed = False
        self.config_error = ""
        self.config = self.load_config()
        self.state = self.load_state()

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {
                "version": 1,
                "toasts": {"enabled": False, "duration": "default"},
                "batchAction": "delete_all",
                "servers": [],
            }
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                return validate_config(json.load(handle))
        except (OSError, json.JSONDecodeError, ConfigError) as error:
            self.config_malformed = True
            self.config_error = bounded_text(error)
            return {
                "version": 1,
                "toasts": {"enabled": False, "duration": "default"},
                "batchAction": "delete_all",
                "servers": [],
            }
    def load_state(self) -> dict[str, Any]:
        empty = {"version": 1, "muteUntil": 0, "servers": {}}
        if not self.state_path.exists():
            return empty
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("servers"), dict):
                raise ValueError("Unsupported state format")
            mute_until = raw.get("muteUntil", 0)
            if not isinstance(mute_until, (int, float)):
                mute_until = 0
            raw["muteUntil"] = int(mute_until)
            return raw
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return empty

    def server_by_id(self, server_id: str) -> dict[str, Any] | None:
        return next((server for server in self.config["servers"] if server["id"] == server_id), None)

    def server_state(self, server_id: str) -> dict[str, Any]:
        servers = self.state.setdefault("servers", {})
        value = servers.setdefault(server_id, {
            "cursor": "", "notifications": {}, "recentIds": [], "dismissed": []
        })
        if not isinstance(value.get("notifications"), dict):
            value["notifications"] = {}
        if not isinstance(value.get("recentIds"), list):
            value["recentIds"] = []
        else:
            del value["recentIds"][:-MAX_RECENT_IDS]
        if not isinstance(value.get("dismissed"), list):
            value["dismissed"] = []
        else:
            del value["dismissed"][:-MAX_DISMISSED]
        return value

    def persist_state(self) -> None:
        with self.state_lock:
            atomic_json_write(self.state_path, self.state)

    def current_mute_until(self) -> int:
        with self.state_lock:
            mute_until = int(self.state.get("muteUntil") or 0)
            if mute_until > 0 and mute_until <= int(time.time()):
                self.state["muteUntil"] = 0
                self.persist_state()
                return 0
            return mute_until

    def set_mute(self, duration_seconds: int) -> None:
        if duration_seconds < 0:
            mute_until = -1
        elif duration_seconds == 0:
            mute_until = 0
        else:
            mute_until = int(time.time()) + min(duration_seconds, 7 * 24 * 60 * 60)
        with self.state_lock:
            self.state["muteUntil"] = mute_until
            self.persist_state()
        self.emit({"event": "mute_status", "muteUntil": mute_until})

    def safe_error(self, error: Any, fallback: str = "Request failed") -> str:
        text = bounded_text(error) or fallback
        for server in self.config.get("servers", []):
            secret = str((server.get("auth") or {}).get("secret") or "")
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[:256]

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.stdout_lock:
            self.output.write(line + "\n")
            self.output.flush()

    @staticmethod
    def _run_notification_sender(argv: list[str]) -> None:
        # Toasts run on their own worker, so a shell that is slow to answer can
        # take its time without holding up the subscription streams.
        subprocess.run(argv, check=True, timeout=15, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    @staticmethod
    def toast_failure_reason(error: BaseException) -> str:
        if isinstance(error, subprocess.TimeoutExpired):
            return f"no reply within {error.timeout:g} seconds"
        if isinstance(error, subprocess.CalledProcessError):
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
            first_line = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
            return first_line[:160] or f"exit status {error.returncode}"
        return str(getattr(error, "strerror", "") or error or type(error).__name__)[:160]

    def toast_image(self, server: dict[str, Any], row: dict[str, Any]) -> str:
        attachment = row.get("attachment") or {}
        url = str(attachment.get("url") or "")
        content_type = str(attachment.get("type") or "")
        expires = int(attachment.get("expires") or row.get("expires") or 0)
        if not url or (content_type and not content_type.startswith("image/")):
            return ""
        if expires and expires <= int(time.time()):
            return ""
        try:
            path = self.download_media(server, "attachment", url)
        except Exception:
            return ""
        self.enforce_media_cache()
        # Eviction removes unreferenced files first, which can include this one.
        return str(path) if path.exists() else ""

    def send_native_toast(self, server: dict[str, Any], row: dict[str, Any]) -> None:
        with self.state_lock:
            current_server = self.server_by_id(str(server.get("id") or ""))
            settings = self.config["toasts"]
            if (not settings["enabled"] or current_server is None
                    or not current_server["enabled"] or not current_server["showToasts"]):
                return
        if self.current_mute_until() != 0:
            return

        summary_source = " ".join(
            str(row.get("title") or "Omartfy").replace("\0", "�").splitlines()
        ).strip()[:160]
        label = " ".join(str(current_server["label"]).replace("\0", "�").splitlines()).strip()
        topic = str(row.get("topic") or "").replace("\0", "�")
        message = str(row.get("message") or "").replace("\0", "�")
        body_source = f"{label} · {topic}\n{message}"[:500]
        if summary_source.startswith("-"):
            summary_source = " " + summary_source
        if body_source.startswith("-"):
            body_source = " " + body_source
        summary = html.escape(summary_source, quote=True)
        body = html.escape(body_source, quote=True)

        duration = str(settings["duration"])
        priority = int(row.get("priority") or 3)
        urgency = "low" if priority <= 2 else ("critical" if priority >= 5 else "normal")
        expire_time = ""
        if duration in {"8-seconds", "15-seconds", "30-seconds"}:
            urgency = "normal"
            expire_time = {
                "8-seconds": "8000",
                "15-seconds": "15000",
                "30-seconds": "30000",
            }[duration]
        elif duration == "until-dismissed":
            urgency = "critical"

        image = self.toast_image(current_server, row)
        argv = [
            "omarchy-notification-send",
            "--app-name", "Omartfy",
            "--glyph", "󰂚",
            "--urgency", urgency,
            *(["--image", image] if image else []),
            summary, body,
        ]
        if expire_time:
            argv.extend(["--expire-time", expire_time])
        # --exec consumes the rest of the argv as the click command, so it goes last.
        argv.extend(["--exec", "omarchy-shell", "shell", "summon", "dailen.omartfy", "{}"])

        try:
            self.notification_sender(argv)
        except (OSError, subprocess.SubprocessError) as error:
            with self.toast_lock:
                should_emit = not self.toast_error_active
                self.toast_error_active = True
            if should_emit:
                self.emit({"event": "error", "fatal": False,
                           "message": "Could not show desktop toast: " + self.toast_failure_reason(error)})
        else:
            with self.toast_lock:
                self.toast_error_active = False

    def set_server_status(self, server_id: str, state: str, error: str = "") -> None:
        safe = self.safe_error(error, "") if error else ""
        with self.state_lock:
            previous = self.statuses.get(server_id)
            current = {"state": state, "error": safe}
            if previous == current:
                return
            self.statuses[server_id] = current
        self.emit({"event": "server_status", "serverId": server_id, "state": state, "error": safe})

    def all_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.state_lock:
            for server in self.config["servers"]:
                state = self.server_state(server["id"])
                for row in state["notifications"].values():
                    if isinstance(row, dict):
                        row["serverLabel"] = server["label"]
                        rows.append(qml_notification(row))
        rows.sort(key=lambda row: (int(row.get("time") or 0), str(row.get("id") or "")), reverse=True)
        return rows

    def snapshot(self) -> dict[str, Any]:
        servers = []
        with self.state_lock:
            for server in self.config["servers"]:
                status = self.statuses.get(server["id"], {
                    "state": "disabled" if not server["enabled"] else "connecting", "error": ""
                })
                servers.append(public_server(server, status["state"], status["error"]))
        return {
            "event": "snapshot",
            "servers": servers,
            "notifications": self.all_rows(),
            "toastSettings": dict(self.config["toasts"]),
            "batchAction": str(self.config.get("batchAction", "delete_all")),
            "muteUntil": self.current_mute_until(),
        }

    def normalize_message(self, server_id: str, event: dict[str, Any]) -> dict[str, Any]:
        server = self.server_by_id(server_id)
        if server is None:
            raise ValueError("Unknown server")
        message_id = str(event.get("id") or "")
        topic = str(event.get("topic") or "")
        if not message_id or not TOPIC_RE.fullmatch(topic):
            raise ValueError("Message is missing an id or valid topic")
        effective_sequence = str(event.get("sequence_id") or message_id)
        actions, protected_actions = normalize_actions(event.get("actions"))
        tags = event.get("tags") if isinstance(event.get("tags"), list) else []
        priority = event.get("priority", 3)
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 3
        if priority not in {1, 2, 3, 4, 5}:
            priority = 3
        try:
            event_time = int(event.get("time") or time.time())
        except (TypeError, ValueError):
            event_time = int(time.time())
        try:
            expires = int(event.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0
        row = {
            "notificationKey": f"{server_id}:{topic}:{effective_sequence}",
            "serverId": server_id,
            "serverLabel": server["label"],
            "id": message_id,
            "effectiveSequence": effective_sequence,
            "time": event_time,
            "expires": expires,
            "topic": topic,
            "message": str(event.get("message") or ""),
            "title": str(event.get("title") or f"{server['label']}/{topic}"),
            "tags": [str(tag) for tag in tags],
            "priority": priority,
            "unread": True,
            "click": str(event.get("click") or ""),
            "actions": actions,
            "attachment": clean_attachment(event.get("attachment")),
            "icon": str(event.get("icon") or ""),
            "contentType": str(event.get("content_type") or ""),
            "iconPath": "",
            "attachmentPath": "",
            "_actions": protected_actions,
        }
        return row

    def apply_event(self, server_id: str, event: dict[str, Any], advance_cursor: bool = True,
                    show_toast: bool = False) -> None:
        event_type = str(event.get("event") or "")
        if event_type in {"open", "keepalive", "poll_request"} or event_type not in {
            "message", "message_clear", "message_delete"
        }:
            return
        event_id = str(event.get("id") or "")
        emitted: dict[str, Any] | None = None
        native_toast: tuple[dict[str, Any], dict[str, Any]] | None = None
        with self.state_lock:
            state = self.server_state(server_id)
            recent = state["recentIds"]
            if event_id and event_id in recent:
                if advance_cursor:
                    state["cursor"] = event_id
                    self.persist_state()
                return
            if event_type == "message":
                try:
                    row = self.normalize_message(server_id, event)
                except ValueError:
                    return
                key = row["notificationKey"]
                dismissed_key = f"{row['topic']}:{row['effectiveSequence']}"
                old = state["notifications"].get(key)
                if old:
                    row["unread"] = bool(old.get("unread", True))
                    row["iconPath"] = str(old.get("iconPath") or "") if old.get("icon") == row["icon"] else ""
                    row["attachmentPath"] = str(old.get("attachmentPath") or "") if old.get("attachment") == row["attachment"] else ""
                if dismissed_key not in state["dismissed"]:
                    state["notifications"][key] = row
                    ordered = sorted(state["notifications"].values(),
                                     key=lambda item: (int(item.get("time") or 0), str(item.get("id") or "")),
                                     reverse=True)
                    state["notifications"] = {
                        item["notificationKey"]: item for item in ordered[:MAX_ROWS]
                    }
                    emitted = {"event": "notification_upsert", "notification": qml_notification(row)}
                    server = self.server_by_id(server_id)
                    if old is None and show_toast and server is not None:
                        native_toast = (server, row)
            else:
                topic = str(event.get("topic") or "")
                effective_sequence = str(event.get("sequence_id") or event_id)
                key = f"{server_id}:{topic}:{effective_sequence}"
                if state["notifications"].pop(key, None) is not None:
                    emitted = {"event": "notification_remove", "notificationKey": key}
            if event_id:
                recent.append(event_id)
                del recent[:-MAX_RECENT_IDS]
                if advance_cursor:
                    state["cursor"] = event_id
            self.persist_state()
        if emitted:
            self.emit(emitted)
        if native_toast:
            self.toast_executor.submit(self.send_native_toast, *native_toast)

    def mark_read(self, server_id: str) -> None:
        changed: list[dict[str, Any]] = []
        with self.state_lock:
            targets = self.config["servers"] if server_id == "all" else [self.server_by_id(server_id)]
            for server in targets:
                if not server:
                    continue
                for row in self.server_state(server["id"])["notifications"].values():
                    if row.get("unread"):
                        row["unread"] = False
                        changed.append(qml_notification(row))
            self.persist_state()
        for row in changed:
            self.emit({"event": "notification_upsert", "notification": row})

    def mark_notification_read(self, notification_key: str) -> bool:
        changed: dict[str, Any] | None = None
        with self.state_lock:
            for server in self.config["servers"]:
                row = self.server_state(server["id"])["notifications"].get(notification_key)
                if row is None:
                    continue
                if row.get("unread"):
                    row["unread"] = False
                    changed = qml_notification(row)
                    self.persist_state()
                break
        if changed:
            self.emit({"event": "notification_upsert", "notification": changed})
        return changed is not None

    def delete_notification(self, notification_key: str) -> bool:
        with self.state_lock:
            for server in self.config["servers"]:
                state = self.server_state(server["id"])
                row = state["notifications"].pop(notification_key, None)
                if row is None:
                    continue
                dismissed = state["dismissed"]
                dismissed_key = f"{row['topic']}:{row['effectiveSequence']}"
                if dismissed_key not in dismissed:
                    dismissed.append(dismissed_key)
                    del dismissed[:-MAX_DISMISSED]
                self.persist_state()
                self.emit({"event": "notification_remove", "notificationKey": notification_key})
                return True
        return False

    def clear(self, server_id: str) -> None:
        removed: list[str] = []
        with self.state_lock:
            targets = self.config["servers"] if server_id == "all" else [self.server_by_id(server_id)]
            for server in targets:
                if not server:
                    continue
                state = self.server_state(server["id"])
                for key, row in list(state["notifications"].items()):
                    dismissed_key = f"{row['topic']}:{row['effectiveSequence']}"
                    if dismissed_key not in state["dismissed"]:
                        state["dismissed"].append(dismissed_key)
                    removed.append(key)
                state["notifications"] = {}
                state["dismissed"] = state["dismissed"][-MAX_DISMISSED:]
            self.persist_state()
        for key in removed:
            self.emit({"event": "notification_remove", "notificationKey": key})

    def stop_worker(self, server_id: str) -> None:
        with self.workers_lock:
            worker = self.workers.pop(server_id, None)
        if worker:
            worker.stop()
            worker.join(timeout=3)

    def start_worker(self, server: dict[str, Any], bootstrap_topics: list[str] | None = None,
                     cursor_override: str | None = None,
                     bootstrap_advance_cursor: bool = False) -> None:
        if not server["enabled"]:
            self.set_server_status(server["id"], "disabled", "")
            return
        worker = SubscriptionWorker(
            self, dict(server), bootstrap_topics, cursor_override, bootstrap_advance_cursor
        )
        with self.workers_lock:
            self.workers[server["id"]] = worker
        worker.start()

    def start_all(self) -> None:
        if self.config_malformed:
            self.emit({"event": "error", "fatal": True,
                       "message": "Malformed ntfy configuration: " + self.config_error})
            return
        for server in self.config["servers"]:
            with self.state_lock:
                cursor = str(self.server_state(server["id"]).get("cursor") or "")
            bootstrap_topics = list(server["topics"]) if server["enabled"] and not cursor else []
            self.start_worker(server, bootstrap_topics,
                              bootstrap_advance_cursor=bool(bootstrap_topics))

    def remove_media_for_rows(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            for field in ("iconPath", "attachmentPath"):
                path = str(row.get(field) or "")
                if path:
                    try:
                        Path(path).unlink()
                    except FileNotFoundError:
                        pass

    def save_server(self, request_id: str, candidate: Any) -> None:
        if self.config_malformed:
            self.emit({"event": "config_result", "requestId": request_id, "operation": "save",
                       "ok": False, "server": {}, "deletedServerId": "",
                       "error": "Malformed configuration must be fixed outside the editor"})
            return
        try:
            candidate_id = str(candidate.get("id") or "") if isinstance(candidate, dict) else ""
            existing = self.server_by_id(candidate_id) if candidate_id else None
            normalized = normalize_server(candidate, existing)
            next_servers = list(self.config["servers"])
            existing_index = next((index for index, item in enumerate(next_servers)
                                   if item["id"] == normalized["id"]), -1)
            if existing_index >= 0:
                next_servers[existing_index] = normalized
            else:
                next_servers.append(normalized)
            labels: set[str] = set()
            for server in next_servers:
                folded = server["label"].casefold()
                if folded in labels:
                    raise ConfigError("Server labels must be unique")
                labels.add(folded)
            next_config = {
                "version": 1,
                "toasts": dict(self.config["toasts"]),
                "batchAction": str(self.config.get("batchAction", "delete_all")),
                "servers": next_servers,
            }

            cursor = ""
            added_topics: list[str] = []
            bootstrap_advance_cursor = False
            self.stop_worker(normalized["id"])
            with self.state_lock:
                state = self.server_state(normalized["id"])
                cursor = str(state.get("cursor") or "")
                if existing:
                    identity_changed = (existing["baseUrl"] != normalized["baseUrl"] or
                                        existing["auth"] != normalized["auth"])
                    if identity_changed:
                        self.remove_media_for_rows(list(state["notifications"].values()))
                        self.state["servers"][normalized["id"]] = {
                            "cursor": "", "notifications": {}, "recentIds": [], "dismissed": []
                        }
                        cursor = ""
                    else:
                        removed_topics = set(existing["topics"]) - set(normalized["topics"])
                        removed_rows = [row for row in state["notifications"].values()
                                        if row.get("topic") in removed_topics]
                        self.remove_media_for_rows(removed_rows)
                        state["notifications"] = {
                            key: row for key, row in state["notifications"].items()
                            if row.get("topic") not in removed_topics
                        }
                        added_topics = [topic for topic in normalized["topics"] if topic not in existing["topics"]]
                        for row in state["notifications"].values():
                            row["serverLabel"] = normalized["label"]
                if not cursor and not added_topics:
                    added_topics = list(normalized["topics"])
                bootstrap_advance_cursor = bool(added_topics and not cursor)
                self.config = next_config
                atomic_json_write(self.config_path, self.config)
                self.persist_state()
            self.statuses.pop(normalized["id"], None)
            self.emit({"event": "config_result", "requestId": request_id, "operation": "save",
                       "ok": True, "server": public_server(normalized), "deletedServerId": "", "error": ""})
            self.emit(self.snapshot())
            self.start_worker(normalized, added_topics, cursor if added_topics else None,
                              bootstrap_advance_cursor)
        except (ConfigError, OSError) as error:
            self.emit({"event": "config_result", "requestId": request_id, "operation": "save",
                       "ok": False, "server": {}, "deletedServerId": "",
                       "error": self.safe_error(error, "Could not save server")})

    def save_toast_settings(self, request_id: str, candidate: Any) -> None:
        if self.config_malformed:
            self.emit({
                "event": "config_result", "requestId": request_id, "operation": "save_toasts",
                "ok": False, "settings": {}, "server": {}, "deletedServerId": "",
                "error": "Malformed configuration must be fixed outside the editor",
            })
            return
        try:
            settings = normalize_toast_settings(candidate)
            with self.state_lock:
                next_config = {
                    "version": 1,
                    "toasts": settings,
                    "batchAction": str(self.config.get("batchAction", "delete_all")),
                    "servers": self.config["servers"],
                }
                atomic_json_write(self.config_path, next_config)
                self.config = next_config
            self.emit({
                "event": "config_result", "requestId": request_id, "operation": "save_toasts",
                "ok": True, "settings": dict(settings), "server": {}, "deletedServerId": "",
                "error": "",
            })
            self.emit(self.snapshot())
        except (ConfigError, OSError) as error:
            self.emit({
                "event": "config_result", "requestId": request_id, "operation": "save_toasts",
                "ok": False, "settings": {}, "server": {}, "deletedServerId": "",
                "error": self.safe_error(error, "Could not save notification settings"),
            })


    def save_batch_action(self, request_id: str, candidate: Any) -> None:
        if self.config_malformed:
            self.emit({
                "event": "config_result", "requestId": request_id, "operation": "save_batch_action",
                "ok": False, "batchAction": "delete_all", "server": {}, "deletedServerId": "",
                "error": "Malformed configuration must be fixed outside the editor",
            })
            return
        try:
            action = normalize_batch_action(candidate)
            with self.state_lock:
                next_config = {
                    "version": 1,
                    "toasts": dict(self.config["toasts"]),
                    "batchAction": action,
                    "servers": self.config["servers"],
                }
                atomic_json_write(self.config_path, next_config)
                self.config = next_config
            self.emit({
                "event": "config_result", "requestId": request_id, "operation": "save_batch_action",
                "ok": True, "batchAction": action, "server": {}, "deletedServerId": "",
                "error": "",
            })
            self.emit(self.snapshot())
        except (ConfigError, OSError) as error:
            self.emit({
                "event": "config_result", "requestId": request_id, "operation": "save_batch_action",
                "ok": False, "batchAction": "delete_all", "server": {}, "deletedServerId": "",
                "error": self.safe_error(error, "Could not save header action setting"),
            })


    def delete_server(self, request_id: str, server_id: str) -> None:
        if self.config_malformed:
            self.emit({"event": "config_result", "requestId": request_id, "operation": "delete",
                       "ok": False, "server": {}, "deletedServerId": "",
                       "error": "Malformed configuration must be fixed outside the editor"})
            return
        server = self.server_by_id(server_id)
        if server is None:
            self.emit({"event": "config_result", "requestId": request_id, "operation": "delete",
                       "ok": False, "server": {}, "deletedServerId": "",
                       "error": "Server profile was not found"})
            return
        self.stop_worker(server_id)
        try:
            with self.state_lock:
                state = self.state["servers"].pop(server_id, None)
                if state:
                    self.remove_media_for_rows(list((state.get("notifications") or {}).values()))
                self.config = {
                    "version": 1,
                    "toasts": dict(self.config["toasts"]),
                    "batchAction": str(self.config.get("batchAction", "delete_all")),
                    "servers": [
                        item for item in self.config["servers"] if item["id"] != server_id
                    ],
                }
                atomic_json_write(self.config_path, self.config)
                self.persist_state()
                self.statuses.pop(server_id, None)
            self.emit({"event": "config_result", "requestId": request_id, "operation": "delete",
                       "ok": True, "server": {}, "deletedServerId": server_id, "error": ""})
            self.emit(self.snapshot())
        except OSError as error:
            self.emit({"event": "config_result", "requestId": request_id, "operation": "delete",
                       "ok": False, "server": {}, "deletedServerId": "",
                       "error": self.safe_error(error, "Could not delete server")})

    def find_row(self, notification_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.state_lock:
            for server in self.config["servers"]:
                row = self.server_state(server["id"])["notifications"].get(notification_key)
                if row:
                    return server, row
        raise ValueError("Notification is no longer available")

    def find_action(self, notification_key: str, action_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        server, row = self.find_row(notification_key)
        if action_id == "__click":
            return server, row, {"id": action_id, "action": "view", "label": "Open", "url": row.get("click", ""), "clear": False, "method": ""}
        if action_id == "__attachment":
            attachment = row.get("attachment") or {}
            expires = int(attachment.get("expires") or row.get("expires") or 0)
            if expires and expires <= int(time.time()):
                raise ValueError("Attachment has expired")
            return server, row, {"id": action_id, "action": "view", "label": "Open attachment",
                                 "url": attachment.get("url", ""), "clear": False, "method": ""}
        action = next((item for item in row.get("_actions", []) if item.get("id") == action_id), None)
        if action is None:
            raise ValueError("Action is no longer available")
        return server, row, action

    def perform_action(self, request_id: str, notification_key: str, action_id: str) -> None:
        pair = (notification_key, action_id)
        with self.state_lock:
            if pair in self.inflight_actions:
                return
            self.inflight_actions.add(pair)
        self.executor.submit(self._perform_action_task, request_id, notification_key, action_id, pair)

    def _perform_action_task(self, request_id: str, notification_key: str,
                             action_id: str, pair: tuple[str, str]) -> None:
        status = 0
        try:
            server, _row, action = self.find_action(notification_key, action_id)
            action_type = action.get("action")
            if action_type == "view":
                url = validate_web_url(action.get("url"))
                subprocess.Popen(["omarchy-launch-browser", url], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                status = 200
            elif action_type == "copy":
                subprocess.run(["wl-copy"], input=str(action.get("value") or "").encode("utf-8"),
                               check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                status = 200
            elif action_type == "http":
                if not server.get("allowHttpActions", False):
                    raise ValueError("Publisher-supplied HTTP actions are disabled for this server")
                status = self.execute_http_action(action)
            else:
                raise ValueError("Unsupported action")
            if action.get("clear"):
                self.delete_notification(notification_key)
            self.emit({"event": "action_result", "requestId": request_id,
                       "notificationKey": notification_key, "actionId": action_id,
                       "ok": True, "status": status, "error": ""})
        except Exception as error:
            self.emit({"event": "action_result", "requestId": request_id,
                       "notificationKey": notification_key, "actionId": action_id,
                       "ok": False, "status": status, "error": self.safe_error(error)})
        finally:
            with self.state_lock:
                self.inflight_actions.discard(pair)

    def execute_http_action(self, action: dict[str, Any]) -> int:
        url = validate_web_url(action.get("url"))
        method = str(action.get("method") or "POST").upper()
        if method not in HTTP_METHODS:
            raise ValueError("Unsupported HTTP action method")
        body_text = str(action.get("body") or "")
        if method in {"GET", "HEAD"} and body_text:
            raise ValueError("GET and HEAD actions cannot include a body")
        headers: dict[str, str] = {}
        raw_headers = action.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise ValueError("Invalid HTTP action headers")
        for raw_name, raw_value in raw_headers.items():
            name, value = str(raw_name), str(raw_value)
            if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                raise ValueError("HTTP action headers cannot contain newlines")
            if value and value[0] in " \t":
                raise ValueError("HTTP action header values cannot start with whitespace")
            if name.lower() in FORBIDDEN_ACTION_HEADERS:
                raise ValueError(f"HTTP action cannot set {name}")
            headers[name] = value
        data = None if not body_text else body_text.encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        if not request.has_header("User-agent"):
            request.add_header("User-Agent", USER_AGENT)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=15) as response:
                status = int(response.status)
                response.read(65537)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            try:
                error.read(65537)
            except Exception:
                pass
            finally:
                error.close()
        if not 200 <= status <= 299:
            raise ValueError(f"HTTP action returned {status}")
        return status

    def test_server(self, request_id: str, candidate: Any) -> None:
        self.executor.submit(self._test_server_task, request_id, candidate)

    def _test_server_task(self, request_id: str, candidate: Any) -> None:
        try:
            candidate_id = str(candidate.get("id") or "") if isinstance(candidate, dict) else ""
            server = normalize_server(candidate, self.server_by_id(candidate_id) if candidate_id else None)
            request = urllib.request.Request(stream_url(server, server["topics"], "latest", True), method="GET",
                                             headers={"User-Agent": USER_AGENT})
            auth = authorization_header(server)
            if auth:
                request.add_header("Authorization", auth)
            with self.urlopen(request, timeout=10) as response:
                status = int(getattr(response, "status", 200))
                saw_json = False
                for line in response:
                    if not line.strip():
                        continue
                    value = json.loads(line.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("Server returned invalid ntfy data")
                    saw_json = True
                if not saw_json:
                    raise ValueError("Server returned no ntfy data")
            self.emit({"event": "test_result", "requestId": request_id,
                       "ok": True, "status": status, "error": ""})
        except urllib.error.HTTPError as error:
            error.close()
            message = "Authentication failed" if error.code in {401, 403} else f"HTTP {error.code}"
            self.emit({"event": "test_result", "requestId": request_id,
                       "ok": False, "status": int(error.code), "error": message})
        except Exception as error:
            self.emit({"event": "test_result", "requestId": request_id,
                       "ok": False, "status": 0, "error": self.safe_error(error, "Connection test failed")})

    def fetch_media(self, request_id: str, notification_key: str, kind: str) -> None:
        self.executor.submit(self._fetch_media_task, request_id, notification_key, kind)

    def _fetch_media_task(self, request_id: str, notification_key: str, kind: str) -> None:
        try:
            if kind not in {"icon", "attachment"}:
                raise MediaError("Unknown media kind")
            server, row = self.find_row(notification_key)
            if kind == "icon":
                url = str(row.get("icon") or "")
            else:
                attachment = row.get("attachment") or {}
                expires = int(attachment.get("expires") or row.get("expires") or 0)
                if expires and expires <= int(time.time()):
                    raise MediaError("Attachment has expired")
                url = str(attachment.get("url") or "")
            if not url:
                raise MediaError("No media URL")
            path = self.download_media(server, kind, url)
            with self.state_lock:
                _server, current = self.find_row(notification_key)
                current["iconPath" if kind == "icon" else "attachmentPath"] = str(path)
                self.persist_state()
            self.enforce_media_cache()
            self.emit({"event": "media_result", "requestId": request_id,
                       "notificationKey": notification_key, "kind": kind,
                       "ok": True, "path": str(path), "error": ""})
        except Exception as error:
            self.emit({"event": "media_result", "requestId": request_id,
                       "notificationKey": notification_key, "kind": kind,
                       "ok": False, "path": "", "error": self.safe_error(error, "Could not load media")})

    def download_media(self, server: dict[str, Any], kind: str, source_url: str) -> Path:
        deadline = time.monotonic() + 8
        url = validate_media_url(server, source_url)
        base_origin = origin_tuple(server["baseUrl"])
        same_origin_opener = urllib.request.build_opener(NoRedirect)
        redirects = 0
        response: Any = None
        while True:
            if time.monotonic() >= deadline:
                raise MediaError("Media request timed out")
            request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
            origin = origin_tuple(url)
            if origin == base_origin:
                opener = same_origin_opener
                auth = authorization_header(server)
                if auth:
                    request.add_header("Authorization", auth)
            else:
                _scheme, hostname, port = origin
                address = resolve_public_address(hostname, port, deadline - time.monotonic())
                opener = pinned_media_opener(address)
            try:
                response = opener.open(request, timeout=max(0.1, deadline - time.monotonic()))
                break
            except urllib.error.HTTPError as error:
                code = error.code
                location = error.headers.get("Location")
                error.close()
                if code in {301, 302, 303, 307, 308}:
                    if not location or redirects >= 3:
                        raise MediaError("Too many media redirects")
                    url = validate_media_url(server, urllib.parse.urljoin(url, location))
                    redirects += 1
                    continue
                raise MediaError(f"Media request returned {code}") from error
        try:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_MEDIA_FILE:
                raise MediaError("Media exceeds 5 MiB")
            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise MediaError("Media request timed out")
                chunk = response.read(min(65536, MAX_MEDIA_FILE + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MEDIA_FILE:
                    raise MediaError("Media exceeds 5 MiB")
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            response.close()
        image_type, width, height = image_info(data)
        allowed = {"jpg", "png"} if kind == "icon" else {"jpg", "png", "gif", "webp"}
        if image_type not in allowed:
            raise MediaError("Unsupported image format")
        if not width or not height or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
            raise MediaError("Image dimensions exceed 4096×4096")
        self.media_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(f"{server['id']}\0{kind}\0{source_url}".encode("utf-8")).hexdigest()
        destination = self.media_dir / f"{digest}.{image_type}"
        fd, temporary = tempfile.mkstemp(prefix=".media.", dir=self.media_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        os.utime(destination, None)
        return destination.resolve()

    def enforce_media_cache(self) -> None:
        if not self.media_dir.exists():
            return
        with self.state_lock:
            references: dict[Path, tuple[dict[str, Any], str]] = {}
            for state in self.state.get("servers", {}).values():
                for row in (state.get("notifications") or {}).values():
                    for field in ("iconPath", "attachmentPath"):
                        value = str(row.get(field) or "")
                        if value:
                            references[Path(value)] = (row, field)
            files = [path for path in self.media_dir.iterdir() if path.is_file() and not path.name.startswith(".")]
            files.sort(key=lambda path: path.stat().st_mtime)
            total = sum(path.stat().st_size for path in files)
            for referenced in (False, True):
                for path in list(files):
                    if total <= MAX_MEDIA_CACHE:
                        break
                    is_referenced = path.resolve() in {item.resolve() for item in references}
                    if is_referenced != referenced:
                        continue
                    size = path.stat().st_size
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    total -= size
                    files.remove(path)
                    if is_referenced:
                        row, field = references[path.resolve()]
                        row[field] = ""
            self.persist_state()

    def reload(self) -> None:
        for server_id in list(self.workers):
            self.stop_worker(server_id)
        self.config_malformed = False
        self.config_error = ""
        self.config = self.load_config()
        self.emit(self.snapshot())
        self.start_all()

    def handle_command(self, command: dict[str, Any]) -> None:
        name = str(command.get("cmd") or "")
        request_id = str(command.get("requestId") or "")
        if name == "reload":
            self.reload()
        elif name == "mark_read":
            self.mark_read(str(command.get("serverId") or "all"))
        elif name == "mark_notification_read":
            self.mark_notification_read(str(command.get("notificationKey") or ""))
        elif name == "delete_notification":
            self.delete_notification(str(command.get("notificationKey") or ""))
        elif name == "clear":
            self.clear(str(command.get("serverId") or "all"))
        elif name == "set_mute":
            try:
                duration = int(command.get("durationSeconds") or 0)
            except (TypeError, ValueError):
                duration = 0
            self.set_mute(duration)
        elif name == "perform_action":
            self.perform_action(request_id, str(command.get("notificationKey") or ""),
                                str(command.get("actionId") or ""))
        elif name == "fetch_media":
            self.fetch_media(request_id, str(command.get("notificationKey") or ""),
                             str(command.get("kind") or ""))
        elif name == "save_server":
            self.save_server(request_id, command.get("server"))
        elif name == "save_batch_action":
            self.save_batch_action(request_id, command.get("batchAction"))
        elif name == "save_toast_settings":
            self.save_toast_settings(request_id, command.get("settings"))
        elif name == "delete_server":
            self.delete_server(request_id, str(command.get("serverId") or ""))
        elif name == "test_server":
            self.test_server(request_id, command.get("server"))
        else:
            self.emit({"event": "error", "fatal": False, "message": "Unknown bridge command"})

    def shutdown(self) -> None:
        self.stop_event.set()
        for server_id in list(self.workers):
            self.stop_worker(server_id)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.toast_executor.shutdown(wait=True, cancel_futures=True)
        self.persist_state()

    def run(self) -> int:
        self.emit(self.snapshot())
        self.start_all()
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                    if not isinstance(command, dict):
                        raise ValueError
                    self.handle_command(command)
                except (json.JSONDecodeError, ValueError):
                    self.emit({"event": "error", "fatal": False, "message": "Ignored malformed bridge command"})
        finally:
            self.shutdown()
        return 0


def main() -> int:
    return Bridge().run()


if __name__ == "__main__":
    raise SystemExit(main())
