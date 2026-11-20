"""Synthetic UI sender regressions; no real Codex, PowerShell, or messages."""
from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/codex-connect/scripts/send_ui.py"
SPEC = importlib.util.spec_from_file_location("send_ui_under_test", SCRIPT)
ui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ui)
THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
TITLE = "Synthetic UI chat"
CWD = "/synthetic/project"
TEXT = "Synthetic message\nKeep trailing spaces  \nand code\\\n"


def state(turns=None):
    return {
        "source": "live_extension_snapshot", "thread_id": THREAD,
        "title": TITLE, "cwd": CWD, "runtime_status": {"type": "idle"},
        "pending_state_known": True, "pending_request_count": 0,
        "pending_request_methods": [], "unconfirmed_submission_count": 0,
        "observation": {"snapshot_version": 13},
        "turns": turns or [],
    }


def turn(identifier="fresh", text=TEXT, stamp=2000, truncated=False):
    return {
        "id": identifier, "status": "completed", "started_at_ms": stamp,
        "messages": [
            {"role": "user", "text": text, "truncated": truncated},
            {"role": "assistant", "text": "Synthetic acknowledgment"},
        ],
    }


@contextlib.contextmanager
def context(message=TEXT):
    with tempfile.TemporaryDirectory(prefix="ui-synthetic-") as directory:
        home = Path(directory)
        codex_home = home / ".codex"
        codex_home.mkdir()
        with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
            connection.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT, cwd TEXT, archived INTEGER)")
            connection.execute("INSERT INTO threads VALUES (?, '', ?, ?, 0)", (THREAD, TITLE, CWD))
        message_file = home / "message.txt"
        message_file.write_bytes(message.encode("utf-8"))
        with mock.patch.object(ui.Path, "home", return_value=home), mock.patch.dict(
            os.environ, {"WSL_DISTRO_NAME": "SyntheticDistro", "XDG_STATE_HOME": str(home / "state")}, clear=False
        ):
            args = ui.parser().parse_args([
                "--thread", THREAD, "--title", TITLE, "--expect-cwd", CWD,
                "--message-file", str(message_file), "--codex-home", str(codex_home),
                "--wait", "0",
            ])
            yield args, home


class TargetAndInputTests(unittest.TestCase):
    def test_unique_title_is_bound_to_explicit_uuid_and_cwd(self):
        with context() as (args, home):
            adapter = ui.load_adapter(args.adapter)
            ui.check_unique_target(adapter, Path(args.codex_home), THREAD, TITLE, CWD)
            with sqlite3.connect(Path(args.codex_home) / "state_5.sqlite") as connection:
                connection.execute("INSERT INTO threads VALUES (?, '', ?, ?, 0)", (OTHER, TITLE, "/synthetic/other"))
            with self.assertRaises(ui.UiSendError) as caught:
                ui.check_unique_target(adapter, Path(args.codex_home), THREAD, TITLE, CWD)
            self.assertEqual(caught.exception.result["adapter_error"], "ambiguous_title")

    def test_unknown_and_unconfirmed_input_never_become_ready(self):
        with context() as (args, _):
            for field, value in (
                ("pending_state_known", False), ("pending_request_methods", None),
                ("pending_request_count", False), ("unconfirmed_submission_count", None),
                ("unconfirmed_submission_count", 1), ("runtime_status", {"type": "active"}),
            ):
                candidate = state()
                candidate[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ui.UiSendError):
                    ui.check_snapshot_ready(candidate, args)

    def test_ui_does_not_expand_to_legacy_or_unknown_snapshot_versions(self):
        with context() as (args, _):
            for observation in (None, {}, {"snapshot_version": 11},
                                {"snapshot_version": "13"}, {"snapshot_version": 14}):
                candidate = state()
                candidate["observation"] = observation
                with self.subTest(observation=observation), self.assertRaises(ui.UiSendError) as caught:
                    ui.check_snapshot_ready(candidate, args)
                self.assertEqual(caught.exception.result["reason"], "ui_snapshot_version_not_supported")

    def test_unknown_or_wrong_context_and_custom_profile_fail_closed(self):
        with context() as (args, home):
            with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": ""}):
                with self.assertRaises(ui.UiSendError):
                    ui.validate_input(copy.copy(args))
            changed = copy.copy(args)
            changed.wsl_distro = "DifferentDistro"
            with self.assertRaises(ui.UiSendError):
                ui.validate_input(changed)
            changed = copy.copy(args)
            changed.codex_home = str(home / "custom-profile")
            with self.assertRaises(ui.UiSendError) as caught:
                ui.validate_input(changed)
            self.assertEqual(caught.exception.result["reason"], "custom_codex_home_not_verified_for_ui")

    def test_message_limits_nul_and_symlink(self):
        with context() as (args, home):
            original = Path(args.message_file)
            original.write_bytes(b"x" * (ui.MAX_MESSAGE_BYTES + 1))
            with self.assertRaises(ui.UiSendError):
                ui.validate_input(copy.copy(args))
            original.write_bytes(b"hello\0world")
            with self.assertRaises(ui.UiSendError):
                ui.validate_input(copy.copy(args))
            original.write_bytes(b"valid")
            link = home / "linked.txt"
            link.symlink_to(original)
            changed = copy.copy(args)
            changed.message_file = str(link)
            with self.assertRaises(ui.UiSendError):
                ui.validate_input(changed)

    def test_wait_accepts_msi_range_and_rejects_unbounded_values(self):
        with context() as (args, _):
            for value in (0, 1, 30, 60):
                changed = copy.copy(args)
                changed.wait = value
                with self.subTest(wait=value):
                    ui.validate_input(changed)
            for value in (-1, 60.01, float("inf"), float("nan")):
                changed = copy.copy(args)
                changed.wait = value
                with self.subTest(wait=value), self.assertRaises(ui.UiSendError) as caught:
                    ui.validate_input(changed)
                self.assertEqual(caught.exception.result["reason"], "wait_out_of_range")

    def test_two_local_senders_do_not_share_the_input_critical_section(self):
        with context():
            with ui.local_sender_lock():
                with self.assertRaises(ui.UiSendError) as caught:
                    with ui.local_sender_lock():
                        self.fail("second sender acquired the lock")
                self.assertEqual(caught.exception.result["reason"], "other_sender_active")
            with ui.local_sender_lock():
                pass


class CorrelationTests(unittest.TestCase):
    def test_new_full_timestamped_user_turn_is_observed(self):
        result = ui.correlate_turn(state([turn()]), set(), TEXT, 1000)
        self.assertEqual(result["phase"], "observed")
        self.assertEqual(result["turn_id"], "fresh")
        self.assertTrue(result["reply_observed"])

    def test_old_page_baseline_truncation_and_missing_id_do_not_confirm(self):
        for candidate, baseline in (
            (turn(stamp=900), set()), (turn(), {"fresh"}),
            (turn(truncated=True), set()), (turn(identifier=None), set()),
        ):
            with self.subTest(candidate=candidate, baseline=baseline):
                self.assertIsNone(ui.correlate_turn(state([candidate]), baseline, TEXT, 1000))

    def test_missing_or_ambiguous_time_stays_unknown(self):
        for turns in ([turn(stamp=None)], [turn(stamp=True)], [turn("one"), turn("two")]):
            with self.subTest(turns=turns):
                result = ui.correlate_turn(state(turns), set(), TEXT, 1000)
                self.assertEqual(result["outcome"], "unknown")

    def test_only_crlf_normalizes_not_spaces_or_code_backslashes(self):
        self.assertEqual(
            ui.correlate_turn(state([turn(text=TEXT.replace("\n", "\r\n"))]), set(), TEXT, 1000)["phase"],
            "observed",
        )
        for text in (TEXT.strip(), TEXT.replace("\\\n", "\n"), TEXT.replace("spaces  ", "spaces")):
            with self.subTest(text=text):
                self.assertIsNone(ui.correlate_turn(state([turn(text=text)]), set(), TEXT, 1000))


class InvocationTests(unittest.TestCase):
    def test_ui_budget_uses_remaining_helper_time_with_return_margins(self):
        budget = ui.ui_time_budget(139, 100, 1700000000)
        self.assertEqual(budget["hard_timeout_seconds"], 38)
        self.assertEqual(budget["soft_deadline_unix_ms"], 1700000036000)
        self.assertEqual(budget["soft_budget_ms"], 36000)
        for available in (-1, 0, 2.9, 3):
            with self.subTest(available=available), self.assertRaises(ui.UiSendError) as caught:
                ui.ui_time_budget(100 + available, 100, 1700000000)
            self.assertEqual(caught.exception.result["error_code"], "UI_BUDGET_EXHAUSTED")

    def test_exhausted_prelaunch_budget_does_not_start_process(self):
        with context() as (args, _):
            ui.validate_input(args)
            with mock.patch.object(ui, "windows_path", side_effect=lambda path, deadline: str(path)), \
                 mock.patch.object(ui.shutil, "which", return_value="synthetic-powershell"), \
                 mock.patch.object(ui.time, "monotonic", return_value=100), \
                 mock.patch.object(ui.subprocess, "run") as call:
                with self.assertRaises(ui.UiSendError) as caught:
                    ui.invoke_ui(args, TEXT.encode(), "a" * 64, 103)
            call.assert_not_called()
            self.assertEqual(caught.exception.result["outcome"], "not_sent")
            self.assertFalse(caught.exception.result["submit_attempted"])

    def test_probe_navigation_can_exceed_old_sixteen_second_cap(self):
        with context() as (args, _):
            ui.validate_input(args)
            args.probe = True
            clock = [100.0]
            def fake_run(argv, **kwargs):
                self.assertIn("-ProbeOnly", argv)
                self.assertEqual(kwargs["timeout"], 38)
                self.assertEqual(argv[argv.index("-UiDeadlineUnixMs") + 1], "1700000036000")
                self.assertEqual(argv[argv.index("-UiBudgetMilliseconds") + 1], "36000")
                clock[0] += 26.245
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "phase": "ui_ready", "outcome": "not_sent", "submit_attempted": False,
                    "last_confirmed_phase": "ui_ready",
                }), "")
            with mock.patch.object(ui, "windows_path", side_effect=lambda path, deadline: str(path)), \
                 mock.patch.object(ui.shutil, "which", return_value="synthetic-powershell"), \
                 mock.patch.object(ui.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(ui.time, "time", return_value=1700000000), \
                 mock.patch.object(ui.subprocess, "run", side_effect=fake_run) as call:
                result = ui.invoke_ui(args, TEXT.encode(), "a" * 64, 139)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(result["phase"], "ui_ready")
            self.assertFalse(result["inner_ui_timed_out"])
            self.assertGreaterEqual(result["ui_elapsed_ms"], 26244)

    def test_ps_timeout_is_unknown_and_never_retries(self):
        with context() as (args, _):
            ui.validate_input(args)
            with mock.patch.object(ui, "windows_path", side_effect=lambda path, deadline: str(path)), \
                 mock.patch.object(ui.shutil, "which", return_value="synthetic-powershell"), \
                 mock.patch.object(ui.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 1)) as call:
                with self.assertRaises(ui.UiSendError) as caught:
                    ui.invoke_ui(args, TEXT.encode(), hashlib.sha256(TEXT.encode()).hexdigest(), time.monotonic() + 10)
                self.assertEqual(caught.exception.result["outcome"], "unknown")
                self.assertIsNone(caught.exception.result["submit_attempted"])
                self.assertEqual(caught.exception.result["error_code"], "UI_PROCESS_TIMEOUT")
                self.assertEqual(caught.exception.result["cause"], "TimeoutExpired")
                self.assertTrue(caught.exception.result["inner_ui_timed_out"])
                self.assertGreaterEqual(caught.exception.result["ui_elapsed_ms"], 0)
                self.assertEqual(call.call_count, 1)

    def test_oserror_without_separate_creation_proof_stays_unknown(self):
        with context() as (args, _):
            ui.validate_input(args)
            with mock.patch.object(ui, "windows_path", side_effect=lambda path, deadline: str(path)), \
                 mock.patch.object(ui.shutil, "which", return_value="synthetic-powershell"), \
                 mock.patch.object(ui.subprocess, "run", side_effect=FileNotFoundError):
                with self.assertRaises(ui.UiSendError) as caught:
                    ui.invoke_ui(args, TEXT.encode(), "a" * 64, time.monotonic() + 10)
                self.assertEqual(caught.exception.result["outcome"], "unknown")
                self.assertIsNone(caught.exception.result["submit_attempted"])
                self.assertEqual(caught.exception.result["error_code"], "UI_PROCESS_IO_ERROR")
                self.assertEqual(caught.exception.result["cause"], "OSError")
                self.assertFalse(caught.exception.result["inner_ui_timed_out"])

    def test_ps_soft_deadline_stays_unknown_even_before_submit(self):
        with context() as (args, _):
            ui.validate_input(args)
            value = {
                "phase": "outcome_unknown", "outcome": "unknown", "submit_attempted": None,
                "last_confirmed_phase": "staging_started", "error_code": "UI_DEADLINE_EXCEEDED",
                "reason": "UI_DEADLINE_EXCEEDED", "inner_ui_timed_out": True,
            }
            with mock.patch.object(ui, "windows_path", side_effect=lambda path, deadline: str(path)), \
                 mock.patch.object(ui.shutil, "which", return_value="synthetic-powershell"), \
                 mock.patch.object(ui.subprocess, "run", return_value=subprocess.CompletedProcess([], 3, json.dumps(value), "")) as call:
                result = ui.invoke_ui(args, TEXT.encode(), "a" * 64, time.monotonic() + 30)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(result["outcome"], "unknown")
            self.assertIsNone(result["submit_attempted"])
            self.assertTrue(result["inner_ui_timed_out"])
            self.assertEqual(result["last_confirmed_phase"], "staging_started")

    def test_communication_oserror_after_child_effect_stays_unknown(self):
        with context() as (args, home):
            ui.validate_input(args)
            marker = home / "synthetic-child-effect.txt"
            original_popen = subprocess.Popen
            original_communicate = original_popen.communicate
            def harmless_child(argv, **kwargs):
                # Replace the complete UI command with an ordinary local Python
                # process. This fixture never starts PowerShell or touches UI.
                command = [sys.executable, "-c",
                           "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran', encoding='utf-8')",
                           str(marker)]
                return original_popen(command, **kwargs)
            def communication_error(process, *values, **kwargs):
                original_communicate(process, *values, **kwargs)
                raise OSError(errno.EIO, "Synthetic pipe error after child effect")
            with mock.patch.object(ui, "windows_path", side_effect=lambda path, deadline: str(path)), \
                 mock.patch.object(ui.shutil, "which", return_value="synthetic-powershell"), \
                 mock.patch.object(ui.subprocess, "Popen", side_effect=harmless_child) as start, \
                 mock.patch.object(original_popen, "communicate", communication_error):
                with self.assertRaises(ui.UiSendError) as caught:
                    ui.invoke_ui(args, TEXT.encode(), "a" * 64, time.monotonic() + 10)
            self.assertEqual(marker.read_text(encoding="utf-8"), "ran")
            self.assertEqual(start.call_count, 1)
            self.assertEqual(caught.exception.result["phase"], "outcome_unknown")
            self.assertEqual(caught.exception.result["outcome"], "unknown")
            self.assertIsNone(caught.exception.result["submit_attempted"])

    def test_exact_file_hash_context_and_safe_child_environment(self):
        with context() as (args, _):
            ui.validate_input(args)
            digest = hashlib.sha256(TEXT.encode()).hexdigest()
            seen = []
            def fake_run(argv, **kwargs):
                seen.append((argv, kwargs))
                self.assertEqual(Path(argv[argv.index("-MessageFile") + 1]).read_bytes(), TEXT.encode())
                self.assertEqual(argv[argv.index("-ExpectedMessageSha256") + 1], digest)
                self.assertEqual(argv[argv.index("-ExpectedWslDistro") + 1], "SyntheticDistro")
                self.assertFalse(any(key.upper() == "PSMODULEPATH" for key in kwargs["env"]))
                self.assertFalse(kwargs.get("shell", False))
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "phase": "ui_invoked", "outcome": "unknown", "submit_attempted": True,
                    "message_sha256": digest, "submit_started_at_ms": int(time.time() * 1000),
                    "last_confirmed_phase": "ui_invoked",
                }), "")
            with mock.patch.object(ui, "windows_path", side_effect=lambda path, deadline: str(path)), \
                 mock.patch.object(ui.shutil, "which", return_value="synthetic-powershell"), \
                 mock.patch.dict(os.environ, {"PSModulePath": "/synthetic/linux/modules"}), \
                 mock.patch.object(ui.subprocess, "run", side_effect=fake_run):
                result = ui.invoke_ui(args, TEXT.encode(), digest, time.monotonic() + 10)
                self.assertEqual(result["phase"], "ui_invoked")
            self.assertEqual(len(seen), 1)
            self.assertFalse(Path(seen[0][0][seen[0][0].index("-MessageFile") + 1]).exists())

    def test_malformed_hash_or_phase_never_proves_delivery(self):
        for output in ("not json", "[]", json.dumps({"phase": "observed"}),
                       json.dumps({"phase": "ui_invoked", "submit_attempted": True, "message_sha256": "b" * 64})):
            with self.subTest(output=output), self.assertRaises(ui.UiSendError) as caught:
                ui.parse_ui_result(subprocess.CompletedProcess([], 0, output, ""), "a" * 64, False)
            self.assertEqual(caught.exception.result["outcome"], "unknown")

    def test_staging_failure_retains_draft_without_claiming_submit(self):
        value = {
            "phase": "staged_not_submitted", "outcome": "not_sent",
            "submit_attempted": False, "last_confirmed_phase": "staging_started",
            "reason": "TARGET_CHANGED_AFTER_STAGING", "draft_preserved": True,
        }
        result = ui.parse_ui_result(subprocess.CompletedProcess([], 3, json.dumps(value), ""), "a" * 64, False)
        self.assertEqual(result, value)

    def test_preflight_failure_never_dispatches_ui(self):
        with context() as (args, _), \
             mock.patch.object(ui, "snapshot", side_effect=ui.UiSendError("snapshot_unavailable")), \
             mock.patch.object(ui, "invoke_ui") as invoke:
            result = ui.run(args)
            self.assertEqual(result["outcome"], "not_sent")
            self.assertFalse(result["submit_attempted"])
            invoke.assert_not_called()

    def test_observation_failure_preserves_one_submit_and_unknown(self):
        with context() as (args, _):
            args.wait = 1
            with mock.patch.object(ui, "snapshot", side_effect=[state(), ui.UiSendError("snapshot_unavailable")]), \
                 mock.patch.object(ui, "invoke_ui", return_value={
                     "phase": "ui_invoked", "submit_attempted": True, "outcome": "unknown",
                     "last_confirmed_phase": "ui_invoked", "enqueue_started_at_ms": 1000,
                 }) as invoke:
                result = ui.run(args)
            self.assertEqual(result["phase"], "ui_invoked_unobserved")
            self.assertTrue(result["submit_attempted"])
            self.assertEqual(invoke.call_count, 1)

    def test_full_send_returns_observed_only_after_reader_confirmation(self):
        with context() as (args, _):
            args.wait = 1
            with mock.patch.object(ui, "snapshot", side_effect=[state([turn("old", stamp=100)]), state([turn()])]), \
                 mock.patch.object(ui, "invoke_ui", return_value={
                     "phase": "ui_invoked", "submit_attempted": True, "outcome": "unknown",
                     "last_confirmed_phase": "ui_invoked", "enqueue_started_at_ms": 1000,
                 }) as invoke:
                result = ui.run(args)
            self.assertEqual(result["phase"], "observed")
            self.assertEqual(invoke.call_count, 1)

    def test_observation_releases_input_lock_before_waiting(self):
        with context() as (args, _):
            args.wait = 1
            calls = 0
            def fake_snapshot(*unused):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return state()
                with ui.local_sender_lock():
                    return state([turn()])
            with mock.patch.object(ui, "snapshot", side_effect=fake_snapshot), \
                 mock.patch.object(ui, "invoke_ui", return_value={
                     "phase": "ui_invoked", "submit_attempted": True, "outcome": "unknown",
                     "last_confirmed_phase": "ui_invoked", "enqueue_started_at_ms": 1000,
                 }):
                result = ui.run(args)
            self.assertEqual(result["phase"], "observed")
            self.assertEqual(calls, 2)

    def test_slow_ui_shortens_observation_to_existing_helper_deadline(self):
        with context() as (args, _):
            args.wait = 15
            clock = [100.0]
            deadlines = []
            def fake_snapshot(unused_args, deadline):
                deadlines.append(deadline)
                return state() if len(deadlines) == 1 else state([turn()])
            def fake_invoke(*unused):
                clock[0] = 137.5
                return {"phase": "ui_invoked", "submit_attempted": True, "outcome": "unknown",
                        "last_confirmed_phase": "ui_invoked", "enqueue_started_at_ms": 1000}
            with mock.patch.object(ui.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(ui, "snapshot", side_effect=fake_snapshot), \
                 mock.patch.object(ui, "invoke_ui", side_effect=fake_invoke):
                result = ui.run(args)
            self.assertEqual(result["phase"], "observed")
            self.assertEqual(deadlines, [139, 139])


if __name__ == "__main__":
    unittest.main()
