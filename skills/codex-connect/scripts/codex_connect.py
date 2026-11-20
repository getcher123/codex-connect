#!/usr/bin/env python3
"""Read a live Codex IDE conversation and queue an explicitly addressed message.

Linux/WSL, Python 3.10+, standard library only. No app-server is started.
The IPC is private and version-bound; native Codex queue handles outbound input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import uuid

EXTENSION_VERSION = "26.1111.21308"
CLI_VERSION = "0.169.0-alpha.11.1"
SNAPSHOT_VERSION = 13
SUPPORTED_BACKENDS = {
    "26.1106.51428": {"cli_version": "0.167.0-alpha.4.1", "snapshot_version": 11,
                      "queue_supported": True},
    "26.1111.21308": {"cli_version": "0.169.0-alpha.11.1", "snapshot_version": 13,
                      "queue_supported": False},
}
SUPPORTED_SNAPSHOT_VERSIONS = frozenset(value["snapshot_version"]
                                      for value in SUPPORTED_BACKENDS.values())
MAX_FRAME = 8 * 1024 * 1024
MAX_RECEIVE_FRAME = 32 * 1024 * 1024
MAX_OUTPUT_CHARS = 262144
UUID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


class BridgeError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def valid_thread(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("invalid_thread", "A complete thread UUID is required.")


def resolve_thread(codex_home, thread_id=None, title=None):
    """Resolve an exact, current display name; never pick the newest row."""
    if bool(thread_id) == bool(title):
        raise BridgeError("target_required", "Specify exactly one thread UUID or exact title.")
    path = (Path(codex_home) / "state_5.sqlite").resolve()
    if not path.is_file():
        raise BridgeError("state_missing", "Codex state database was not found.", path=str(path))
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            where, value = ("id = ?", valid_thread(thread_id)) if thread_id else (
                "COALESCE(NULLIF(name,''),title) = ?", title)
            rows = conn.execute(
                "SELECT id,name,title,cwd,archived FROM threads WHERE " + where + " LIMIT 3",
                (value,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BridgeError("state_unreadable", "Cannot read the expected Codex state schema.") from exc
    if not rows:
        raise BridgeError("thread_not_found", "No thread matches the explicit target.")
    if len(rows) != 1:
        raise BridgeError("ambiguous_title", "Several threads have this title; choose a UUID.",
                          matches=[dict(row) for row in rows])
    return dict(rows[0])


class IPCClient:
    def __init__(self, socket_path, timeout=5.0, deadline=None):
        self.path, self.timeout = str(socket_path), timeout
        self.deadline = deadline
        self.phase = "connect"
        self.last_observation = None
        self.sock = None
        self.client_id = None
        self.following = set()
        self.observed_threads = set()
        self.buffer = bytearray()

    def __enter__(self):
        if not hasattr(socket, "AF_UNIX"):
            raise BridgeError("unsupported_platform", "Run the adapter inside Linux/WSL.")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(self.remaining())
            self.sock.connect(self.path)
            result = self.request("initialize", 0, {"clientType": "codex-connect"},
                                  source="initializing-client")
            payload = result.get("result")
            self.client_id = payload.get("clientId") if isinstance(payload, dict) else None
            if not isinstance(self.client_id, str) or not self.client_id:
                raise BridgeError("invalid_handshake", "IPC did not return a client identity.")
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *args):
        self.close()

    def remaining(self, timeout=None):
        remaining = self.timeout if timeout is None else timeout
        if self.deadline is not None:
            remaining = min(remaining, self.deadline - time.monotonic())
        if remaining <= 0:
            raise BridgeError("ipc_timeout", "The bounded IPC operation deadline expired.",
                              phase=self.phase)
        return remaining

    def send_frame(self, message):
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise BridgeError("frame_too_large", "Outgoing IPC frame exceeds the size limit.")
        self.sock.sendall(struct.pack("<I", len(raw)) + raw)

    def _fill(self, count, deadline):
        # Preserve a partial frame across a bounded discovery/poll timeout.
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("IPC deadline exceeded")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(min(65536, count - len(self.buffer)))
            if not chunk:
                raise BridgeError("ipc_closed", "The extension closed the IPC connection.")
            self.buffer.extend(chunk)

    def receive(self, deadline):
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        while True:
            self._fill(4, deadline)
            size = struct.unpack("<I", self.buffer[:4])[0]
            if not 0 < size <= MAX_RECEIVE_FRAME:
                raise BridgeError("frame_too_large", "Invalid IPC frame size.", size=size,
                                  maximum=MAX_RECEIVE_FRAME)
            self._fill(size + 4, deadline)
            raw = bytes(self.buffer[4:size + 4])
            del self.buffer[:size + 4]
            try:
                message = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise BridgeError("invalid_frame", "IPC frame is not a JSON object.") from exc
            if not isinstance(message, dict):
                raise BridgeError("invalid_frame", "IPC frame is not a JSON object.")
            if "params" in message and not isinstance(message["params"], dict):
                raise BridgeError("invalid_frame", "IPC params must be an object.")
            if message.get("type") == "client-discovery-request":
                self.send_frame({"type": "client-discovery-response",
                                 "requestId": message.get("requestId"),
                                 "response": {"canHandle": False}})
                continue
            params = message.get("params") or {}
            if (message.get("type") == "broadcast"
                    and message.get("method") == "thread-stream-following-changed"
                    and params.get("hostId") == "local" and params.get("following") is True):
                candidate = params.get("conversationId")
                if isinstance(candidate, str) and re.fullmatch(UUID_PATTERN, candidate):
                    self.observed_threads.add(candidate)
            return message

    def request(self, method, version, params, source=None):
        self.phase = method
        remaining = self.remaining()
        self.sock.settimeout(remaining)
        request_id = str(uuid.uuid4())
        self.send_frame({"type": "request", "requestId": request_id,
                         "sourceClientId": source or self.client_id, "version": version,
                         "method": method, "params": params, "timeoutMs": max(1, int(remaining * 1000))})
        deadline = time.monotonic() + remaining
        while True:
            try:
                message = self.receive(deadline)
            except (TimeoutError, socket.timeout) as exc:
                raise BridgeError("ipc_timeout", "The IPC request did not answer before its deadline.",
                                  phase=method) from exc
            if message.get("type") == "response" and message.get("requestId") == request_id:
                if message.get("method") != method or message.get("resultType") != "success":
                    raise BridgeError("ipc_request_failed", "IPC request was not handled successfully.",
                                      method=method, result_type=message.get("resultType"))
                return message

    def owner(self, thread_id):
        result = self.request("thread-owner-discovery", 1,
                              {"hostId": "local", "conversationId": valid_thread(thread_id)})
        owner = result.get("handledByClientId")
        if not isinstance(owner, str) or not owner:
            raise BridgeError("owner_not_found", "No live owner was identified for this thread.")
        return owner

    def _follow(self, thread_id, owner, following):
        self.send_frame({"type": "broadcast", "method": "thread-stream-following-changed",
                         "sourceClientId": self.client_id, "targetClientIds": [owner], "version": 1,
                         "params": {"conversationId": thread_id, "hostId": "local", "following": following}})

    def snapshot(self, thread_id, owner=None, timeout=None):
        thread_id = valid_thread(thread_id)
        owner = owner or self.owner(thread_id)
        self.phase = "snapshot"
        remaining = self.remaining(timeout)
        self.sock.settimeout(remaining)
        self.following.add((thread_id, owner))
        self._follow(thread_id, owner, True)
        deadline = time.monotonic() + remaining
        while True:
            try:
                message = self.receive(deadline)
            except (TimeoutError, socket.timeout) as exc:
                raise BridgeError("ipc_timeout", "No matching live snapshot arrived before its deadline.",
                                  phase="snapshot") from exc
            params = message.get("params") or {}
            if not (message.get("type") == "broadcast"
                    and message.get("method") == "thread-stream-state-changed"
                    and message.get("sourceClientId") == owner
                    and params.get("conversationId") == thread_id
                    and params.get("hostId") == "local"):
                continue
            change = params.get("change")
            if not isinstance(change, dict):
                raise BridgeError("invalid_frame", "IPC state change must be an object.")
            if message.get("version") not in SUPPORTED_SNAPSHOT_VERSIONS:
                raise BridgeError("unsupported_protocol", "Unknown snapshot protocol version.",
                                  observed=message.get("version"), expected=sorted(SUPPORTED_SNAPSHOT_VERSIONS))
            if change.get("type") != "snapshot":
                continue
            state = change.get("conversationState")
            if not isinstance(state, dict) or state.get("id") != thread_id:
                raise BridgeError("target_mismatch", "Snapshot identity does not match the requested thread.")
            self.last_observation = {
                "received_at_ms": int(time.time() * 1000),
                "snapshot_version": message["version"],
                "freshness": "received_in_current_request",
                "source_timestamp_known": False,
            }
            return state

    def collect_followed(self, duration=1.0):
        deadline = time.monotonic() + self.remaining(duration)
        try:
            while time.monotonic() < deadline:
                self.receive(deadline)
        except TimeoutError:
            pass
        return sorted(self.observed_threads)

    def close(self):
        if self.sock is None:
            return
        cleanup_deadline = time.monotonic() + 0.2
        for thread_id, owner in self.following:
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self.sock.settimeout(remaining)
                self._follow(thread_id, owner, False)
            except (OSError, BridgeError):
                break
        self.sock.close()
        self.sock = None


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("text", "") for part in value
                       if isinstance(part, dict) and part.get("type") in ("text", "inputText", "input_text")
                       and isinstance(part.get("text"), str))
    return ""


def extract_turns(state):
    """Normalize only visible user/agent messages, never reasoning or tool output."""
    if not isinstance(state, dict):
        raise BridgeError("invalid_snapshot", "Conversation state must be an object.")
    turn_history = state.get("turnHistory")
    turn_history = {} if turn_history is None else turn_history
    if not isinstance(turn_history, dict):
        raise BridgeError("invalid_snapshot", "Turn history must be an object.")
    history = turn_history.get("history")
    history = {} if history is None else history
    if not isinstance(history, dict):
        raise BridgeError("invalid_snapshot", "Paginated history must be an object.")
    entities = history.get("entitiesByKey")
    entities = {} if entities is None else entities
    live_turns = state.get("turns")
    live_turns = [] if live_turns is None else live_turns
    if not isinstance(entities, dict) or not isinstance(live_turns, list):
        raise BridgeError("invalid_snapshot", "Turn containers do not match the known schema.")
    raw = list(entities.values())
    # When both representations exist, the live turn wins over a cached entity.
    raw.extend(live_turns)
    unique = {}
    for turn in raw:
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("turnId") or turn.get("id")
        if not isinstance(turn_id, str):
            continue
        messages = []
        has_user = False
        items = turn.get("items")
        items = [] if items is None else items
        if not isinstance(items, list):
            raise BridgeError("invalid_snapshot", "Turn items must be an array.")
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "userMessage":
                has_user = True
                text = content_text(item.get("content")) or content_text(item.get("text"))
                messages.append({"role": "user", "id": item.get("id"), "text": text})
            elif kind == "agentMessage":
                messages.append({"role": "assistant", "id": item.get("id"),
                                 "text": content_text(item.get("text"))})
        if not has_user:
            params = turn.get("params")
            params = {} if params is None else params
            if not isinstance(params, dict):
                raise BridgeError("invalid_snapshot", "Turn parameters must be an object.")
            text = content_text(params.get("input"))
            if text:
                messages.insert(0, {"role": "user", "id": None, "text": text})
        started = turn.get("turnStartedAtMs")
        if started is None:
            started = turn.get("startedAtMs")
        unique[turn_id] = {"id": turn_id, "status": turn.get("status"),
                           "started_at_ms": started,
                           "messages": messages}
    return sorted(unique.values(), key=lambda turn: turn["started_at_ms"] if valid_timestamp(turn["started_at_ms"]) else 0)


def valid_timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 2 ** 53


def summarize_metadata(state, observation=None):
    """Read-only metadata; malformed/missing pending state must never become []."""
    if not isinstance(state, dict):
        raise BridgeError("invalid_snapshot", "Conversation state must be an object.")
    requests = state.get("requests")
    pending_known = isinstance(requests, list) and all(
        isinstance(item, dict) and isinstance(item.get("method"), str)
        for item in requests)
    # This field is optional in the validated v11/v13 schema. Malformed values
    # remain unknown, and a nonempty container always blocks a new submission.
    unconfirmed = state.get("unconfirmedTurnSubmissions", [])
    unconfirmed_count = len(unconfirmed) if isinstance(unconfirmed, (list, dict)) else None
    status = state.get("threadRuntimeStatus")
    result = {
        "source": "live_extension_snapshot", "thread_id": state.get("id"),
        "title": state.get("title"), "cwd": state.get("cwd"),
        "runtime_status": status if isinstance(status, dict) else None,
        "pending_request_methods": [item["method"] for item in requests] if pending_known else None,
        "pending_request_count": len(requests) if isinstance(requests, list) else None,
        "unconfirmed_submission_count": unconfirmed_count,
        "pending_state_known": pending_known and unconfirmed_count is not None,
        "history_mode": state.get("historyMode"), "ui_focus_confirmed": False,
    }
    if isinstance(observation, dict):
        result["observation"] = {key: observation[key] for key in (
            "received_at_ms", "snapshot_version", "freshness", "source_timestamp_known")
            if key in observation}
    return result


def summarize_state(state, limit=3, max_chars=2000, observation=None):
    turns = extract_turns(state)
    shown = []
    # Reserve the output budget for the newest turn first. Earlier history must
    # not truncate the new user message we are trying to correlate.
    budget = min(MAX_OUTPUT_CHARS, max(60000, max_chars + 16000))
    for turn in reversed(turns[-limit:] if limit else []):
        messages = []
        selected = turn["messages"][-40:]
        users = [message for message in turn["messages"] if message["role"] == "user"]
        if users and not any(message["role"] == "user" for message in selected):
            selected = users[:1] + selected[-39:]
        for message in selected:
            text = message["text"][:min(max_chars, budget)]
            budget -= len(text)
            messages.append({**message, "text": text, "truncated": len(text) < len(message["text"])})
        shown.append({**turn, "messages": messages})
    return {**summarize_metadata(state, observation), "loaded_turn_count": len(turns),
            "turns": list(reversed(shown))}


def detect_bundled_cli(explicit=None):
    candidates = {}
    proc = Path("/proc")
    if proc.is_dir():
        for pid in proc.iterdir():
            if not pid.name.isdigit():
                continue
            try:
                args = (pid / "cmdline").read_bytes().split(b"\0")
                # Resolve executables only for app-server candidates. Resolving
                # every /proc executable can enter unrelated mounted filesystems.
                if b"app-server" not in args:
                    continue
                binary = (pid / "exe").resolve(strict=True)
                if not any(p.startswith("openai.chatgpt-") for p in binary.parts):
                    continue
                candidates.setdefault(str(binary), []).append(int(pid.name))
            except (OSError, RuntimeError):
                continue
    if explicit:
        selected = str(Path(explicit).expanduser().resolve())
        if selected not in candidates:
            raise BridgeError("binary_not_active", "The selected binary does not match a live extension app-server.")
    elif len(candidates) == 1:
        selected = next(iter(candidates))
    elif not candidates:
        raise BridgeError("backend_not_found", "No live Codex extension app-server was found in /proc.")
    else:
        raise BridgeError("ambiguous_backend", "Several extension binaries are active; select one explicitly.",
                          candidates=sorted(candidates))
    match = re.search(r"/openai\.chatgpt-(\d+\.\d+\.\d+)-[^/]+/bin/[^/]+/codex$", selected)
    compatibility = SUPPORTED_BACKENDS.get(match.group(1)) if match else None
    if compatibility is None:
        raise BridgeError("unsupported_extension", "This extension version has not been validated.",
                          expected=sorted(SUPPORTED_BACKENDS), observed=match.group(1) if match else None)
    try:
        version = subprocess.run([selected, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("version_unknown", "Cannot verify the bundled CLI version.") from exc
    if version.returncode != 0 or version.stdout.strip() != "codex-cli " + compatibility["cli_version"]:
        raise BridgeError("unsupported_cli", "The bundled CLI version does not match the validated version.",
                          expected=compatibility["cli_version"])
    return {"path": selected, "extension_version": match.group(1), **compatibility,
            "pids": sorted(candidates[selected]), "compatible": True}


def queue_message(binary, thread_id, message, codex_home=None):
    """One attempt only. A timeout can occur after acceptance: never retry here."""
    env = None
    if codex_home is not None:
        env = dict(os.environ, CODEX_HOME=str(Path(codex_home).resolve()))
    try:
        result = subprocess.run([str(binary), "queue", "--thread", valid_thread(thread_id), "--message", message],
                                capture_output=True, text=True, timeout=10, env=env)
    except (subprocess.TimeoutExpired, OSError, UnicodeError) as exc:
        raise BridgeError("send_outcome_unknown", "Queue attempt has no confirmed outcome; inspect before retrying.",
                          phase="outcome_unknown", retry_safe=False) from exc
    match = re.search(r"Queued message (" + UUID_PATTERN + r") for thread (" + UUID_PATTERN + r")\.", result.stdout)
    if result.returncode != 0 or not match or match.group(2).lower() != thread_id.lower():
        raise BridgeError("send_outcome_unknown", "Queue command did not confirm acceptance; inspect before retrying.",
                          phase="outcome_unknown", return_code=result.returncode, retry_safe=False)
    return {"phase": "queued", "queue_item_id": match.group(1), "thread_id": thread_id,
            "retry_safe": False}


def check_send_target(state, record, expected_cwd=None, requested_title=None):
    if not isinstance(state, dict):
        raise BridgeError("invalid_snapshot", "Live state must be an object.")
    if state.get("id") != record["id"]:
        raise BridgeError("target_mismatch", "Live thread differs from the resolved target.")
    if not state.get("cwd") or state["cwd"] != record.get("cwd"):
        raise BridgeError("cwd_mismatch", "Live and stored working directories do not match.")
    if expected_cwd is not None and state["cwd"] != expected_cwd:
        raise BridgeError("cwd_mismatch", "Live working directory differs from the expected directory.")
    if requested_title is not None and state.get("title") != requested_title:
        raise BridgeError("title_changed", "The live title changed; select the target again.")
    status = state.get("threadRuntimeStatus")
    if not isinstance(status, dict) or status.get("type") != "idle":
        raise BridgeError("thread_not_idle", "Sending is supported only for a confirmed idle thread.")
    if not isinstance(state.get("requests"), list):
        raise BridgeError("pending_state_unknown", "The snapshot does not identify pending requests.")
    if ("unconfirmedTurnSubmissions" in state
            and not isinstance(state["unconfirmedTurnSubmissions"], (list, dict))):
        raise BridgeError("pending_state_unknown", "Unconfirmed submission state is not recognized.")
    if state["requests"] or state.get("unconfirmedTurnSubmissions"):
        raise BridgeError("pending_input", "The thread has pending input or an unconfirmed submission.")


def correlate_reply(state, baseline_ids, message, not_before_ms=None):
    matches = [turn for turn in extract_turns(state) if turn["id"] not in baseline_ids
               and any(item["role"] == "user" and item["text"] == message for item in turn["messages"])]
    if not_before_ms is not None:
        unknown = [turn for turn in matches if not valid_timestamp(turn["started_at_ms"])]
        matches = [turn for turn in matches if valid_timestamp(turn["started_at_ms"])
                   and turn["started_at_ms"] >= not_before_ms]
        if unknown:
            return {"phase": "ambiguous" if matches or len(unknown) > 1 else "queued",
                    "observation": "matching_turn_timestamp_unknown",
                    "matching_turn_ids": [turn["id"] for turn in matches + unknown], "retry_safe": False}
    if len(matches) > 1:
        return {"phase": "ambiguous", "matching_turn_ids": [turn["id"] for turn in matches]}
    if not matches:
        return None
    turn = matches[0]
    replies = [item for item in turn["messages"] if item["role"] == "assistant"]
    phase = "completed" if turn["status"] == "completed" and replies else "observed"
    return {"phase": phase, "turn_id": turn["id"], "turn_status": turn["status"],
            "replies": [{**item, "text": item["text"][:16000]} for item in replies[-10:]]}


def wait_for_reply(client, thread_id, owner, baseline_ids, message, seconds, not_before_ms=None):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            state = client.snapshot(thread_id, owner, timeout=min(client.timeout, deadline - time.monotonic()))
            matched = correlate_reply(state, baseline_ids, message, not_before_ms)
            if matched:
                last = matched
                if matched["phase"] in ("completed", "ambiguous") or matched.get("turn_status") in ("failed", "interrupted"):
                    return matched
        except Exception as exc:
            # Queue acceptance is already known: keep its receipt even if an
            # unexpected observation/schema error occurs, and never requeue.
            return {"phase": last["phase"] if last else "queued", "observation_error": getattr(exc, "code", type(exc).__name__),
                    "observed": last, "retry_safe": False}
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    return last or {"phase": "queued", "observation": "no_matching_turn_before_deadline", "retry_safe": False}


def run(args):
    codex_home = Path(args.codex_home).expanduser().resolve()
    socket_path = Path(args.socket).expanduser() if args.socket else codex_home / "ipc" / "ipc.sock"
    if args.command == "doctor":
        result = {"codex_home": str(codex_home), "socket": str(socket_path),
                  "socket_exists": socket_path.is_socket(), "state_exists": (codex_home / "state_5.sqlite").is_file(),
                  "validated_snapshot_version": SNAPSHOT_VERSION,
                  "validated_snapshot_versions": sorted(SUPPORTED_SNAPSHOT_VERSIONS)}
        try:
            result["backend"] = detect_bundled_cli(args.codex_bin)
        except BridgeError as exc:
            result["backend"] = {"compatible": False, "error": exc.code, **exc.details}
        result["ipc"] = {"status": "unavailable", "reason": "socket_missing", "scope": "handshake_only"}
        if result["socket_exists"]:
            try:
                budget = min(args.timeout, 3.0)
                with IPCClient(socket_path, budget, deadline=time.monotonic() + budget):
                    result["ipc"] = {"status": "responsive", "scope": "handshake_only",
                                     "received_at_ms": int(time.time() * 1000)}
            except (BridgeError, OSError, TimeoutError) as exc:
                result["ipc"] = {"status": "unavailable", "scope": "handshake_only",
                                 "reason": getattr(exc, "code", type(exc).__name__)}
        result["healthy"] = result["backend"].get("compatible") is True and result["ipc"]["status"] == "responsive"
        return result
    if args.command == "send" and socket_path.resolve() != (codex_home / "ipc" / "ipc.sock").resolve():
        raise BridgeError("socket_home_mismatch", "Sending requires the socket inside the selected Codex home.")
    record = None
    if args.command in ("read", "status", "send") or args.title:
        record = resolve_thread(codex_home, getattr(args, "thread", None), args.title)
    # Reading validates the received snapshot protocol itself. It does not need
    # /proc enumeration or a subprocess running the outbound CLI's --version.
    backend = detect_bundled_cli(args.codex_bin) if args.command == "send" else None
    if args.command == "send" and backend.get("queue_supported") is not True:
        raise BridgeError("queue_transport_unsupported", "Native queue delivery is not validated for this backend; use the verified UI sender.",
                          phase="failed_before_send", retry_safe=True)
    with IPCClient(socket_path, args.timeout, deadline=time.monotonic() + args.timeout) as client:
        if args.command == "discover":
            candidates = [record["id"]] if record else client.collect_followed(min(1.0, args.timeout / 4))
            results = []
            for thread_id in candidates[:args.limit]:
                try:
                    state = client.snapshot(thread_id)
                    summary = summarize_state(state, limit=0, observation=client.last_observation)
                    summary.pop("turns")
                    results.append(summary)
                except (BridgeError, OSError, TimeoutError) as exc:
                    results.append({"thread_id": thread_id, "error": getattr(exc, "code", type(exc).__name__)})
            return {"source": "live_ui_followed_candidates", "ui_focus_confirmed": False, "threads": results,
                    "truncated": len(candidates) > args.limit}
        owner = client.owner(record["id"])
        state = client.snapshot(record["id"], owner)
        if args.command == "status":
            return {**summarize_metadata(state, client.last_observation), "history_inspected": False}
        if args.command == "read":
            return summarize_state(state, args.limit, args.max_chars, client.last_observation)
        check_send_target(state, record, args.expect_cwd, args.title)
        message = args.message
        if args.message_file is not None:
            path = Path(args.message_file)
            if not path.is_file():
                raise BridgeError("invalid_message_file", "Message input must be a regular UTF-8 file.")
            if path.stat().st_size > 65536:
                raise BridgeError("message_too_large", "Message files are limited to 64 KiB.")
            message = path.read_bytes().decode("utf-8")
        if not isinstance(message, str) or not message.strip() or "\0" in message:
            raise BridgeError("invalid_message", "Provide a nonempty text message without NUL bytes.")
        if len(message.encode("utf-8")) > 65536:
            raise BridgeError("message_too_large", "Messages are limited to 64 KiB.")
        baseline = {turn["id"] for turn in extract_turns(state)}
        receipt = {"thread_id": record["id"], "baseline_turn_ids": sorted(baseline),
                   "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()}
        receipt["enqueue_started_at_ms"] = int(time.time() * 1000)
        # The --wait observation has its own explicit budget, after preflight.
        # Expiration of the initial read budget must not trigger a resend.
        client.deadline = None
        try:
            queued = queue_message(backend["path"], record["id"], message, codex_home)
        except BridgeError as exc:
            exc.details.update(receipt)
            raise
        queued.update(receipt)
        if args.wait:
            queued.update(wait_for_reply(client, record["id"], owner, baseline, message, args.wait,
                                         not_before_ms=receipt["enqueue_started_at_ms"]))
        return queued


def bounded(low, high, kind=int):
    def parse(value):
        number = kind(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"Must be between {low} and {high}.")
        return number
    return parse


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    p.add_argument("--socket", help="Explicit existing extension Unix socket")
    p.add_argument("--codex-bin", help="Select one active bundled extension binary")
    p.add_argument("--timeout", type=bounded(1, 60, float), default=5.0,
                   help="Overall initial IPC read/discovery deadline in seconds")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Inspect safe local metadata; no session starts")
    discover = commands.add_parser("discover", help="List live UI-retained candidates, not keyboard focus")
    discover.add_argument("--title", help="Exact current display name")
    discover.add_argument("--limit", type=bounded(1, 200), default=10)
    for name in ("read", "status", "send"):
        command = commands.add_parser(name)
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--thread", help="Explicit thread UUID")
        target.add_argument("--title", help="Exact unique current display name")
        if name == "read":
            command.add_argument("--limit", type=bounded(1, 20), default=3)
            command.add_argument("--max-chars", type=bounded(100, 200000), default=2000)
        elif name == "send":
            text = command.add_mutually_exclusive_group(required=True)
            text.add_argument("--message")
            text.add_argument("--message-file", help="UTF-8 text file; copied exactly, not executed")
            command.add_argument("--expect-cwd", help="Require this exact live working directory")
            command.add_argument("--wait", type=bounded(0, 55, float), default=0)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except BridgeError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc), **exc.details}, ensure_ascii=False, indent=2))
        return 3 if exc.code == "send_outcome_unknown" else 2
    except (OSError, TimeoutError, ValueError) as exc:
        print(json.dumps({"error": "local_io_error", "message": type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
