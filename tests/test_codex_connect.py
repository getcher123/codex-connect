"""Offline regression tests for Codex Connect.

Run from the repository root with ``python3 -m unittest discover -s tests -v``.
Every peer, database, thread, and message in this file is synthetic. These tests
never open the real Codex home, invoke Codex, or contact a remote service.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "codex-connect" / "scripts" / "codex_connect.py"
SPEC = importlib.util.spec_from_file_location("codex_connect_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cc
SPEC.loader.exec_module(cc)

THREAD_ID = "11111111-1111-4111-8111-111111111111"
OTHER_THREAD_ID = "22222222-2222-4222-8222-222222222222"
OWNER = "synthetic-owner"
CWD = "/synthetic/project"
TITLE = "Exact synthetic title"
QUEUE_ID = "33333333-3333-4333-8333-333333333333"


@contextlib.contextmanager
def synthetic_home():
    with tempfile.TemporaryDirectory(prefix="cc-home-") as directory:
        home = Path(directory)
        with sqlite3.connect(home / "state_5.sqlite") as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, cwd TEXT, archived INTEGER)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                (THREAD_ID, "", TITLE, CWD, 0),
            )
        yield home


def visible_turn(turn_id="new-turn", prompt="Synthetic question", reply="Synthetic answer", started=2):
    return {
        "turnId": turn_id,
        "status": "completed",
        "turnStartedAtMs": started,
        "params": {"input": []},
        "items": [
            {"type": "userMessage", "id": "user-" + turn_id,
             "clientId": "synthetic-client-not-the-queue-id",
             "content": [{"type": "text", "text": prompt, "text_elements": []}],
             "startedAtMs": started, "completedAtMs": started},
            {"type": "agentMessage", "id": "agent-" + turn_id, "text": reply},
        ],
    }


def live_state(turns=None):
    return {
        "id": THREAD_ID, "title": TITLE, "cwd": CWD,
        "threadRuntimeStatus": {"type": "idle"}, "requests": [],
        "historyMode": "paginated", "turns": turns or [],
        "turnHistory": {"history": {"entitiesByKey": {}}},
    }


def snapshot_frame(state, owner=OWNER, host="local", thread_id=THREAD_ID, version=11):
    return {
        "type": "broadcast", "method": "thread-stream-state-changed",
        "sourceClientId": owner, "version": version,
        "params": {"hostId": host, "conversationId": thread_id,
                   "change": {"type": "snapshot", "conversationState": state}},
    }


def protocol_handler(snapshots):
    def respond(request):
        if request.get("type") == "request":
            method = request["method"]
            response = {
                "type": "response", "requestId": request["requestId"],
                "method": method, "resultType": "success",
            }
            if method == "initialize":
                response["result"] = {"clientId": "synthetic-reader"}
                return [{"type": "client-discovery-request", "requestId": "discovery-probe"}, response]
            if method == "thread-owner-discovery":
                response["handledByClientId"] = OWNER
                return [response]
            raise AssertionError("Unexpected request method: " + str(method))
        if (request.get("method") == "thread-stream-following-changed"
                and request["params"]["following"]):
            return snapshots
        return []
    return respond


def wire_frame(value):
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def read_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        chunk = stream.recv(length - len(data))
        if not chunk:
            if not data:
                return None
            raise EOFError("The test client closed a partial frame")
        data.extend(chunk)
    return bytes(data)


class FakePeer:
    """One local AF_UNIX peer, with a callback for each decoded client frame."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.errors = []
        self.directory = tempfile.TemporaryDirectory(prefix="cc-peer-")
        self.path = Path(self.directory.name) / "ipc.sock"
        try:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError:
            self.directory.cleanup()
            raise
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.connection = None
        self.worker = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.worker.start()
        return self

    def _serve(self):
        try:
            self.connection, _ = self.listener.accept()
            with self.connection as stream:
                stream.settimeout(2)
                while True:
                    header = read_exact(stream, 4)
                    if header is None:
                        return
                    size = struct.unpack("<I", header)[0]
                    if size > 1024 * 1024:
                        raise AssertionError("Unexpectedly large client fixture frame")
                    payload = read_exact(stream, size)
                    if payload is None:
                        raise EOFError("The test client omitted its JSON payload")
                    request = json.loads(payload.decode("utf-8"))
                    self.requests.append(request)
                    for reply in self.handler(request) or []:
                        frame = reply if isinstance(reply, bytes) else wire_frame(reply)
                        # Split inside the uint32 prefix and the JSON payload.
                        # The peer must work even when recv() returns short data.
                        for first, last in ((0, 1), (1, 3), (3, 9), (9, len(frame))):
                            if last > first:
                                stream.sendall(frame[first:last])
        except (BrokenPipeError, ConnectionResetError):
            # A client rejecting a deliberately bad frame may close immediately.
            pass
        except BaseException as error:
            self.errors.append(error)

    def __exit__(self, exc_type, exc, traceback):
        self.worker.join(timeout=1)
        if self.worker.is_alive() and self.connection is not None:
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        self.worker.join(timeout=3)
        self.directory.cleanup()


class QueueRegressionTests(unittest.TestCase):
    def test_uncertain_queue_outcomes_never_resubmit(self):
        message = "A literal prompt with $HOME, `echo synthetic`, quotes ' and ; characters."
        outcomes = [
            subprocess.TimeoutExpired("codex", 10),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic invalid queue confirmation"),
            subprocess.CompletedProcess(["codex"], 1, "", "synthetic queue failure"),
            subprocess.CompletedProcess(["codex"], 0, "unrecognized output", ""),
            subprocess.CompletedProcess(["codex"], 0,
                f"Queued message {QUEUE_ID} for thread {OTHER_THREAD_ID}.\n", ""),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__, detail=str(outcome)):
                options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with mock.patch.object(cc.subprocess, "run", **options) as invoke:
                    with self.assertRaises(cc.BridgeError) as raised:
                        cc.queue_message("/synthetic/bin/codex", THREAD_ID, message)
                self.assertEqual(raised.exception.code, "send_outcome_unknown")
                self.assertEqual(raised.exception.details["phase"], "outcome_unknown")
                self.assertFalse(raised.exception.details["retry_safe"])
                self.assertEqual(invoke.call_count, 1, "An uncertain queue attempt must never resubmit")
                self.assertEqual(invoke.call_args.args[0],
                    ["/synthetic/bin/codex", "queue", "--thread", THREAD_ID, "--message", message])
                self.assertFalse(invoke.call_args.kwargs.get("shell", False))

    def test_confirmed_queue_uses_exact_argument_and_explicit_home(self):
        message = "line one\n$HOME `echo synthetic` ; 'quotes' — конец\n"
        result = subprocess.CompletedProcess(["codex"], 0,
            f"Queued message {QUEUE_ID} for thread {THREAD_ID}.\n", "")
        with synthetic_home() as home:
            with mock.patch.object(cc.subprocess, "run", return_value=result) as invoke:
                accepted = cc.queue_message("/synthetic/bin/codex", THREAD_ID, message, home)
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(invoke.call_args.args[0],
                ["/synthetic/bin/codex", "queue", "--thread", THREAD_ID, "--message", message])
            self.assertEqual(invoke.call_args.kwargs["env"]["CODEX_HOME"], str(home.resolve()))
            self.assertFalse(invoke.call_args.kwargs.get("shell", False))
        self.assertEqual(accepted["phase"], "queued")
        self.assertEqual(accepted["queue_item_id"], QUEUE_ID)
        self.assertFalse(accepted["retry_safe"])


class DiscoveryRegressionTests(unittest.TestCase):
    def test_exact_title_is_resolved_readonly_and_duplicates_are_rejected(self):
        with synthetic_home() as home:
            database = home / "state_5.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                    (OTHER_THREAD_ID, "", "exact synthetic title", "/synthetic/other", 0),
                )
            before = database.read_bytes()
            original_connect = sqlite3.connect
            with mock.patch.object(cc.sqlite3, "connect", wraps=original_connect) as connect:
                resolved = cc.resolve_thread(home, title=TITLE)
            self.assertEqual(resolved["id"], THREAD_ID)
            self.assertEqual(database.read_bytes(), before)
            self.assertTrue(connect.called)
            for call in connect.call_args_list:
                self.assertIn("mode=ro", str(call.args[0]))
                self.assertTrue(call.kwargs.get("uri"), "SQLite URI read-only mode must be enabled")

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE threads SET title = ? WHERE id = ?",
                    (TITLE, OTHER_THREAD_ID),
                )
            with self.assertRaises(cc.BridgeError) as raised:
                cc.resolve_thread(home, title=TITLE)
            self.assertEqual(raised.exception.code, "ambiguous_title")
            with sqlite3.connect(database) as connection:
                connection.execute("DELETE FROM threads WHERE id = ?", (OTHER_THREAD_ID,))
                connection.execute("UPDATE threads SET name = ? WHERE id = ?", ("Renamed synthetic title", THREAD_ID))
            with self.assertRaises(cc.BridgeError) as raised:
                cc.resolve_thread(home, title=TITLE)
            self.assertEqual(raised.exception.code, "thread_not_found")
            self.assertEqual(cc.resolve_thread(home, title="Renamed synthetic title")["id"], THREAD_ID)
            for target in ({}, {"thread_id": THREAD_ID, "title": "Renamed synthetic title"}, {"thread_id": "latest"}):
                with self.subTest(target=target), self.assertRaises(cc.BridgeError):
                    cc.resolve_thread(home, **target)


class IPCRegressionTests(unittest.TestCase):
    def test_fragmented_socket_end_to_end_read_handshake_and_unsubscribe(self):
        state = live_state([visible_turn(prompt="UTF-8 вопрос", reply="Visible ответ")])
        state["turnHistory"]["history"]["entitiesByKey"]["old"] = visible_turn(
            "old-turn", "Earlier question", "Earlier answer", started=1)
        with synthetic_home() as home, FakePeer(protocol_handler([snapshot_frame(state)])) as peer:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cc.main(["--codex-home", str(home), "--socket", str(peer.path),
                                  "--timeout", "1", "read", "--thread", THREAD_ID])
            result = json.loads(output.getvalue())
        self.assertEqual(status, 0, result)
        self.assertFalse(peer.errors, peer.errors)
        self.assertEqual(result["thread_id"], THREAD_ID)
        self.assertEqual([turn["id"] for turn in result["turns"]], ["old-turn", "new-turn"])
        self.assertEqual(result["turns"][-1]["messages"][0]["text"], "UTF-8 вопрос")
        requests = [request for request in peer.requests if request.get("type") == "request"]
        self.assertEqual([(request["method"], request["version"]) for request in requests],
                         [("initialize", 0), ("thread-owner-discovery", 1)])
        self.assertEqual(requests[0]["params"], {"clientType": "codex-connect"})
        self.assertEqual(requests[1]["params"], {"hostId": "local", "conversationId": THREAD_ID})
        follows = [request for request in peer.requests if request.get("method") == "thread-stream-following-changed"]
        self.assertEqual([request["params"]["following"] for request in follows], [True, False])
        self.assertTrue(all(request["targetClientIds"] == [OWNER] for request in follows))
        self.assertTrue(all(request["version"] == 1 for request in follows))
        self.assertTrue(all(request["params"]["hostId"] == "local"
                            and request["params"]["conversationId"] == THREAD_ID for request in follows))
        self.assertTrue(any(request.get("response") == {"canHandle": False} for request in peer.requests))

    def test_wrong_owner_host_or_thread_cannot_poison_snapshot(self):
        poisoned = live_state([visible_turn(reply="FORGED-SNAPSHOT")])
        trusted = live_state([visible_turn(reply="Trusted answer")])
        replies = [
            snapshot_frame(poisoned, owner="another-owner"),
            snapshot_frame(poisoned, host="remote-host"),
            snapshot_frame(poisoned, thread_id=OTHER_THREAD_ID),
            snapshot_frame(trusted),
        ]
        with FakePeer(protocol_handler(replies)) as peer:
            with cc.IPCClient(peer.path, timeout=1) as client:
                state = client.snapshot(THREAD_ID)
        self.assertFalse(peer.errors, peer.errors)
        self.assertIn("Trusted answer", json.dumps(cc.summarize_state(state)))
        self.assertNotIn("FORGED-SNAPSHOT", json.dumps(cc.summarize_state(state)))

    def test_wrong_nested_identity_and_unknown_snapshot_version_are_refused(self):
        wrong_state = live_state()
        wrong_state["id"] = OTHER_THREAD_ID
        cases = [
            (snapshot_frame(wrong_state), "target_mismatch"),
            (snapshot_frame(live_state(), version=12), "unsupported_protocol"),
        ]
        for reply, code in cases:
            with self.subTest(code=code):
                with FakePeer(protocol_handler([reply])) as peer:
                    with cc.IPCClient(peer.path, timeout=1) as client:
                        with self.assertRaises(cc.BridgeError) as raised:
                            client.snapshot(THREAD_ID)
                    self.assertEqual(raised.exception.code, code)
                self.assertFalse(peer.errors, peer.errors)

    def test_zero_oversized_and_nonobject_frames_are_rejected(self):
        cases = [
            (struct.pack("<I", 0), "frame_too_large"),
            (struct.pack("<I", cc.MAX_FRAME + 1), "frame_too_large"),
            (struct.pack("<I", 1) + b"{", "invalid_frame"),
            (wire_frame(["not", "an", "object"]), "invalid_frame"),
        ]
        for frame, code in cases:
            with self.subTest(code=code, size=len(frame)):
                with FakePeer(lambda request: [frame]) as peer:
                    with self.assertRaises(cc.BridgeError) as raised:
                        with cc.IPCClient(peer.path, timeout=1):
                            pass
                    self.assertEqual(raised.exception.code, code)
                self.assertFalse(peer.errors, peer.errors)


class StateRegressionTests(unittest.TestCase):
    def test_paginated_real_content_live_precedence_and_privacy(self):
        turn = visible_turn(prompt="Exact queued input", reply="Final visible reply")
        turn["items"].extend([
            {"type": "reasoning", "text": "PRIVATE-REASONING"},
            {"type": "commandExecution", "output": "PRIVATE-TOOL-OUTPUT"},
        ])
        state = live_state([turn])
        stale = visible_turn(prompt="Exact queued input", reply="STALE-REPLY")
        stale["status"] = "inProgress"
        state["turnHistory"]["history"]["entitiesByKey"] = {
            "earlier": visible_turn("earlier", "Historical question", "Historical answer", 1),
            "new-turn": stale,
        }
        state["settings"] = {"secret": "PRIVATE-SETTINGS"}
        summary = cc.summarize_state(state, limit=3)
        encoded = json.dumps(summary)
        self.assertEqual(summary["loaded_turn_count"], 2)
        self.assertEqual(summary["turns"][-1]["status"], "completed")
        self.assertEqual(summary["turns"][-1]["messages"][0]["text"], "Exact queued input")
        for text in ("Historical answer", "Final visible reply"):
            self.assertIn(text, encoded)
        for text in ("PRIVATE-REASONING", "PRIVATE-TOOL-OUTPUT", "PRIVATE-SETTINGS", "STALE-REPLY"):
            self.assertNotIn(text, encoded)

    def test_send_requires_known_idle_pending_state_and_consistent_target(self):
        record = {"id": THREAD_ID, "cwd": CWD}
        cc.check_send_target(live_state(), record, CWD, TITLE)
        missing = [("missing " + key, key, None, True)
                   for key in ("id", "cwd", "threadRuntimeStatus", "requests")]
        invalid = [
            ("wrong thread", "id", OTHER_THREAD_ID, False),
            ("wrong cwd", "cwd", "/synthetic/wrong", False),
            ("busy", "threadRuntimeStatus", {"type": "running"}, False),
            ("unknown status", "threadRuntimeStatus", {"type": "future-status"}, False),
            ("malformed status", "threadRuntimeStatus", "idle", False),
            ("null requests", "requests", None, False),
            ("object requests", "requests", {}, False),
            ("pending approval", "requests", [{"method": "synthetic-approval"}], False),
            ("pending submission", "unconfirmedTurnSubmissions", ["synthetic-input"], False),
            ("malformed pending", "unconfirmedTurnSubmissions", False, False),
        ]
        for label, key, value, remove in missing + invalid:
            with self.subTest(label=label):
                state = live_state()
                if remove:
                    del state[key]
                else:
                    state[key] = value
                with self.assertRaises(cc.BridgeError):
                    cc.check_send_target(state, record, CWD, TITLE)
        with self.assertRaises(cc.BridgeError):
            cc.check_send_target(live_state(), record, "/synthetic/wrong", TITLE)
        with self.assertRaises(cc.BridgeError):
            cc.check_send_target(live_state(), record, CWD, "Old synthetic title")

    def test_reply_correlation_uses_new_turn_exact_content_and_its_own_reply(self):
        prompt = "Repeated synthetic prompt"
        old = visible_turn("old", prompt, "Old answer", 1)
        new = visible_turn("new", prompt, "Correct new answer", 2)
        unrelated = visible_turn("unrelated", "Different prompt", "Unrelated answer", 3)
        state = live_state([old, new, unrelated])
        result = cc.correlate_reply(state, {"old"}, prompt)
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(result["turn_id"], "new")
        self.assertEqual([reply["text"] for reply in result["replies"]], ["Correct new answer"])
        self.assertIsNone(cc.correlate_reply(state, {"old", "new"}, prompt))
        state["turns"].append(visible_turn("duplicate", prompt, "Duplicate answer", 4))
        ambiguous = cc.correlate_reply(state, {"old"}, prompt)
        self.assertEqual(ambiguous["phase"], "ambiguous")
        self.assertEqual(set(ambiguous["matching_turn_ids"]), {"new", "duplicate"})


class SendIntegrationRegressionTests(unittest.TestCase):
    def test_unrelated_socket_is_rejected_for_send_but_allowed_for_read(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.owner.return_value = OWNER
        client.snapshot.return_value = live_state([visible_turn()])
        with synthetic_home() as home, tempfile.TemporaryDirectory(prefix="cc-other-home-") as other:
            custom_socket = Path(other) / "ipc.sock"
            # Exercise the default CODEX_HOME as well as the explicit socket.
            with mock.patch.dict(cc.os.environ, {"CODEX_HOME": str(home)}):
                send_args = cc.parser().parse_args(["--socket", str(custom_socket), "send", "--thread", THREAD_ID,
                                                   "--message", "Synthetic prompt"])
                read_args = cc.parser().parse_args(["--socket", str(custom_socket), "read", "--thread", THREAD_ID])
            with mock.patch.object(cc, "IPCClient", return_value=client) as make_client, \
                    mock.patch.object(cc, "detect_bundled_cli", return_value={"path": "/synthetic/bin/codex"}), \
                    mock.patch.object(cc, "queue_message", return_value={"phase": "queued"}) as queue:
                with self.assertRaises(cc.BridgeError) as raised:
                    cc.run(send_args)
                self.assertEqual(raised.exception.code, "socket_home_mismatch")
                queue.assert_not_called()
                make_client.assert_not_called()
                observed = cc.run(read_args)
                self.assertEqual(observed["thread_id"], THREAD_ID)
                self.assertEqual(Path(make_client.call_args.args[0]), custom_socket)
                queue.assert_not_called()

    def test_paged_old_or_undated_matching_turn_cannot_confirm_a_new_send(self):
        prompt = "Repeated paginated prompt"
        state = live_state()
        old_turn = visible_turn("previously-unloaded", prompt, "An old answer", started=999)
        state["turnHistory"]["history"]["entitiesByKey"]["old-page"] = old_turn
        self.assertIsNone(cc.correlate_reply(state, set(), prompt, not_before_ms=1000))

        undated_turn = visible_turn("undated", prompt, "An undated answer")
        del undated_turn["turnStartedAtMs"]
        for item in undated_turn["items"]:
            item.pop("startedAtMs", None)
            item.pop("completedAtMs", None)
        undated = cc.correlate_reply(live_state([undated_turn]), set(), prompt, not_before_ms=1000)
        self.assertIsNotNone(undated)
        self.assertEqual(undated["phase"], "queued")
        self.assertNotIn("replies", undated)
        fresh = cc.correlate_reply(
            live_state([visible_turn("fresh", prompt, "Fresh answer", started=1001)]),
            set(), prompt, not_before_ms=1000)
        self.assertEqual(fresh["phase"], "completed")
        self.assertEqual(fresh["turn_id"], "fresh")

        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.owner.return_value = OWNER
        client.timeout = 1
        client.snapshot.side_effect = [live_state(), state, TimeoutError("synthetic end of observation")]
        with synthetic_home() as home:
            args = cc.parser().parse_args(["--codex-home", str(home), "send", "--thread", THREAD_ID,
                                           "--message", prompt, "--wait", "1"])
            with mock.patch.object(cc.time, "time", return_value=1.0) as clock, \
                    mock.patch.object(cc.time, "sleep"), \
                    mock.patch.object(cc, "IPCClient", return_value=client), \
                    mock.patch.object(cc, "detect_bundled_cli", return_value={"path": "/synthetic/bin/codex"}), \
                    mock.patch.object(cc, "queue_message") as queue:
                def accept_once(*args, **kwargs):
                    clock.return_value = 2.0
                    return {"phase": "queued", "queue_item_id": QUEUE_ID, "retry_safe": False}
                queue.side_effect = accept_once
                receipt = cc.run(args)
        self.assertEqual(queue.call_count, 1)
        self.assertEqual(receipt["enqueue_started_at_ms"], 1000, "Capture freshness before the queue invocation")
        self.assertEqual(receipt["phase"], "queued")
        self.assertEqual(receipt["queue_item_id"], QUEUE_ID)
        self.assertNotIn("replies", receipt)

    def test_malformed_post_acceptance_history_preserves_queue_receipt(self):
        malformed = live_state()
        malformed["turnHistory"] = ["malformed"]
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.owner.return_value = OWNER
        client.timeout = 1
        client.snapshot.side_effect = [live_state(), malformed, TimeoutError("synthetic end of observation")]
        accepted = {"phase": "queued", "queue_item_id": QUEUE_ID, "thread_id": THREAD_ID, "retry_safe": False}
        with synthetic_home() as home:
            args = cc.parser().parse_args(["--codex-home", str(home), "send", "--thread", THREAD_ID,
                                           "--message", "Synthetic prompt", "--wait", "1"])
            with mock.patch.object(cc.time, "sleep"), \
                    mock.patch.object(cc, "IPCClient", return_value=client), \
                    mock.patch.object(cc, "detect_bundled_cli", return_value={"path": "/synthetic/bin/codex"}), \
                    mock.patch.object(cc, "queue_message", return_value=accepted) as queue:
                receipt = cc.run(args)
        self.assertEqual(queue.call_count, 1)
        self.assertEqual(receipt["queue_item_id"], QUEUE_ID)
        self.assertEqual(receipt["thread_id"], THREAD_ID)
        self.assertEqual(receipt["phase"], "queued")
        self.assertTrue(receipt["observation_error"])
        self.assertFalse(receipt["retry_safe"])

    def test_message_file_preserves_utf8_bytes_and_crlf(self):
        original = "First line\r\nВторая строка\r\nLast line\r\n".encode("utf-8")
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.owner.return_value = OWNER
        client.snapshot.return_value = live_state()
        accepted = {"phase": "queued", "queue_item_id": QUEUE_ID, "retry_safe": False}
        with synthetic_home() as home:
            message_file = home / "synthetic-message.txt"
            message_file.write_bytes(original)
            args = cc.parser().parse_args(["--codex-home", str(home), "send", "--thread", THREAD_ID,
                                           "--message-file", str(message_file)])
            with mock.patch.object(cc, "IPCClient", return_value=client), \
                    mock.patch.object(cc, "detect_bundled_cli", return_value={"path": "/synthetic/bin/codex"}), \
                    mock.patch.object(cc, "queue_message", return_value=accepted) as queue:
                cc.run(args)
        self.assertEqual(queue.call_count, 1)
        self.assertEqual(queue.call_args.args[2].encode("utf-8"), original)

    def test_wait_observation_failure_does_not_queue_again(self):
        prompt = "One synthetic submission"
        baseline = live_state([visible_turn("old", prompt, "Previous answer", 1)])
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.owner.return_value = OWNER
        client.timeout = 1
        client.snapshot.side_effect = [baseline, TimeoutError("synthetic observation timeout")]
        accepted = subprocess.CompletedProcess(["codex"], 0,
            f"Queued message {QUEUE_ID} for thread {THREAD_ID}.\n", "")
        with synthetic_home() as home:
            args = cc.parser().parse_args(["--codex-home", str(home), "send", "--thread", THREAD_ID,
                                           "--message", prompt, "--wait", "1", "--expect-cwd", CWD])
            with mock.patch.object(cc, "IPCClient", return_value=client), \
                    mock.patch.object(cc, "detect_bundled_cli", return_value={"path": "/synthetic/bin/codex"}), \
                    mock.patch.object(cc.subprocess, "run", return_value=accepted) as invoke:
                result = cc.run(args)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result["phase"], "queued")
        self.assertEqual(result["queue_item_id"], QUEUE_ID)
        self.assertEqual(result["observation_error"], "TimeoutError")
        self.assertEqual(result["baseline_turn_ids"], ["old"])
        self.assertFalse(result["retry_safe"])

    def test_unknown_or_incompatible_backend_cannot_reach_queue(self):
        with synthetic_home() as home:
            args = cc.parser().parse_args(["--codex-home", str(home), "send", "--thread", THREAD_ID,
                                           "--message", "Synthetic prompt"])
            for code in ("backend_not_found", "ambiguous_backend", "unsupported_extension", "unsupported_cli", "version_unknown"):
                with self.subTest(code=code), \
                        mock.patch.object(cc, "detect_bundled_cli", side_effect=cc.BridgeError(code, "Synthetic backend failure")), \
                        mock.patch.object(cc, "IPCClient") as client, \
                        mock.patch.object(cc, "queue_message") as queue:
                    with self.assertRaises(cc.BridgeError) as raised:
                        cc.run(args)
                    self.assertEqual(raised.exception.code, code)
                    queue.assert_not_called()
                    client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
