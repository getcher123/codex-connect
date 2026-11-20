#!/usr/bin/env python3
"""One guarded UI send to an existing Codex chat in the current WSL context.

This helper never retries a UI submission. The calling bridge must persist its
operation ID before launching it and reconcile uncertain results by that ID.
Only the ordinary UI submits input; the sibling adapter reads live IPC state.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

MAX_MESSAGE_BYTES = 32768
SCRIPT_DIR = Path(__file__).resolve().parent
PLACEHOLDER = "Ask for follow-up changes"


class UiSendError(Exception):
    def __init__(self, code, *, phase="failed_before_send", outcome="not_sent",
                 submit_attempted=False, last_confirmed_phase="preflight",
                 **details):
        super().__init__(code)
        self.result = dict(
            phase=phase, outcome=outcome, reason=code, error_code=code,
            submit_attempted=submit_attempted,
            last_confirmed_phase=last_confirmed_phase, retry_safe=False, **details,
        )


def remaining(deadline, cap):
    value = min(cap, deadline - time.monotonic())
    if value <= 0:
        raise UiSendError("preflight_deadline_exceeded")
    return value


def run_read_command(argv, deadline, cap=6):
    """A bounded read-only subprocess. Do not include output in public errors."""
    try:
        call = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="strict",
            timeout=remaining(deadline, cap), check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise UiSendError("read_subprocess_unavailable",
                          cause=type(exc).__name__) from exc
    if call.returncode:
        raise UiSendError("read_subprocess_failed", return_code=call.returncode)
    return call.stdout


def load_adapter(path):
    spec = importlib.util.spec_from_file_location("codex_connect_ui_reader", path)
    if spec is None or spec.loader is None:
        raise UiSendError("adapter_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_unique_target(adapter, codex_home, thread, title, cwd):
    """UI labels need a unique binding in the exact reader's state database."""
    try:
        explicit = adapter.resolve_thread(codex_home, thread_id=thread)
        named = adapter.resolve_thread(codex_home, title=title)
    except adapter.BridgeError as exc:
        raise UiSendError("target_binding_unavailable",
                          adapter_error=exc.code) from exc
    if (explicit.get("id") != thread or named.get("id") != thread
            or explicit.get("cwd") != cwd or named.get("cwd") != cwd
            or explicit.get("archived") or named.get("archived")):
        raise UiSendError("target_binding_mismatch")


def check_snapshot_identity(state, args):
    if (not isinstance(state, dict)
            or state.get("source") != "live_extension_snapshot"
            or state.get("thread_id") != args.thread
            or state.get("title") != args.title
            or state.get("cwd") != args.expect_cwd):
        raise UiSendError("snapshot_identity_mismatch")


def check_snapshot_ready(state, args):
    check_snapshot_identity(state, args)
    observation = state.get("observation")
    if (not isinstance(observation, dict)
            or type(observation.get("snapshot_version")) is not int
            or observation["snapshot_version"] != 13):
        raise UiSendError("ui_snapshot_version_not_supported")
    if not isinstance(state.get("runtime_status"), dict) or state["runtime_status"].get("type") != "idle":
        raise UiSendError("thread_not_idle")
    if (state.get("pending_state_known") is not True
            or state.get("pending_request_methods") != []
            or type(state.get("pending_request_count")) is not int
            or state["pending_request_count"] != 0
            or type(state.get("unconfirmed_submission_count")) is not int
            or state["unconfirmed_submission_count"] != 0):
        raise UiSendError("pending_or_unknown_input")
    if not isinstance(state.get("turns"), list):
        raise UiSendError("history_state_unknown")


def snapshot(args, deadline):
    # One maximum-size message must fit without truncation. The adapter budgets
    # newest turns first; its output is still bounded independently of history.
    timeout = min(5.0, max(1.0, remaining(deadline, 6) - 1.0))
    output = run_read_command(
        [sys.executable, str(args.adapter), "--codex-home", str(args.codex_home),
         "--timeout", str(timeout), "read", "--thread", args.thread,
         "--limit", "6", "--max-chars", str(MAX_MESSAGE_BYTES)],
        deadline, cap=timeout + 1,
    )
    try:
        result = json.loads(output)
    except ValueError as exc:
        raise UiSendError("snapshot_output_invalid") from exc
    check_snapshot_identity(result, args)
    return result


def normalize_ui_text(text):
    """Normalize platform line endings only; preserve whitespace and code."""
    return text.replace("\r\n", "\n")


def correlate_turn(state, baseline_ids, text, started_at_ms):
    """A historic page, truncated text, or missing time never proves delivery."""
    matches, uncertain = [], []
    for turn in state.get("turns", []):
        if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str)
                or not turn["id"] or turn["id"] in baseline_ids):
            continue
        items = turn.get("messages")
        if not isinstance(items, list):
            continue
        found = any(
            isinstance(item, dict) and item.get("role") == "user"
            and item.get("truncated") is not True
            and isinstance(item.get("text"), str)
            and normalize_ui_text(item["text"]) == normalize_ui_text(text)
            for item in items
        )
        if not found:
            continue
        stamp = turn.get("started_at_ms")
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not math.isfinite(stamp) or not 0 <= stamp < 2 ** 53):
            uncertain.append(turn.get("id"))
        elif stamp >= started_at_ms:
            matches.append(turn)
    if uncertain or len(matches) > 1:
        return dict(phase="outcome_unknown", outcome="unknown",
                    reason="ambiguous_delivery_observation", retry_safe=False)
    if not matches:
        return None
    turn = matches[0]
    responded = any(
        isinstance(item, dict) and item.get("role") == "assistant"
        and isinstance(item.get("text"), str) and item["text"].strip()
        for item in turn["messages"]
    )
    return dict(
        phase="observed", outcome="delivered", turn_id=turn["id"],
        turn_status=turn.get("status"), reply_observed=bool(responded),
        last_confirmed_phase="delivery_observed", retry_safe=False,
        correlation_normalization="crlf_to_lf",
    )


def windows_path(path, deadline):
    executable = shutil.which("wslpath")
    if not executable:
        raise UiSendError("wslpath_unavailable")
    result = run_read_command([executable, "-w", str(path)], deadline, cap=3).strip()
    if not result or "\n" in result or "\r" in result or "\0" in result:
        raise UiSendError("windows_path_unavailable")
    return result


@contextlib.contextmanager
def local_sender_lock():
    # One cooperative lock per Linux user, independent of CODEX_HOME/config.
    # The Windows script additionally locks the shared desktop across distros.
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    lock_path = state_home / "codex-connect" / "send-ui.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UiSendError("other_sender_active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse_ui_result(call, digest, probe):
    try:
        value = json.loads(call.stdout.lstrip("\ufeff").strip())
    except (ValueError, AttributeError) as exc:
        raise UiSendError(
            "ui_result_unavailable", phase="outcome_unknown", outcome="unknown",
            submit_attempted=None, last_confirmed_phase="ui_dispatched",
        ) from exc
    if not isinstance(value, dict):
        raise UiSendError("ui_result_invalid", phase="outcome_unknown",
                          outcome="unknown", submit_attempted=None,
                          last_confirmed_phase="ui_dispatched")
    phase = value.get("phase")
    if value.get("message_sha256") not in (None, digest):
        raise UiSendError("ui_result_hash_mismatch", phase="outcome_unknown",
                          outcome="unknown", submit_attempted=None,
                          last_confirmed_phase="ui_dispatched")
    if (phase in ("failed_before_send", "staged_not_submitted")
            and value.get("outcome") == "not_sent"
            and value.get("submit_attempted") is False):
        return value
    if (probe and call.returncode == 0 and phase == "ui_ready"
            and value.get("outcome") == "not_sent"
            and value.get("submit_attempted") is False):
        return value
    if (not probe and phase == "ui_invoked"
            and value.get("submit_attempted") is True
            and value.get("message_sha256") == digest):
        return value
    if phase == "outcome_unknown" and value.get("outcome") == "unknown":
        return value
    raise UiSendError("ui_result_unconfirmed", phase="outcome_unknown",
                      outcome="unknown", submit_attempted=None,
                      last_confirmed_phase="ui_dispatched")


def invoke_ui(args, message, digest, deadline):
    powershell = args.powershell or shutil.which("powershell.exe")
    if not powershell:
        raise UiSendError("windows_powershell_unavailable")
    script_path = windows_path(args.ui_script, deadline)
    # Windows can read this path through WSL's ordinary filesystem bridge.
    # No user-specific Windows home or temp path is embedded in the package.
    with tempfile.TemporaryDirectory(prefix="codex-ui-send-") as directory:
        message_path = Path(directory) / "message.txt"
        message_path.write_bytes(message)
        message_path.chmod(0o600)
        win_message = windows_path(message_path, deadline)
        command = [
            powershell, "-NoProfile", "-NonInteractive", "-File", script_path,
            "-ExpectedTitle", args.title, "-ExpectedWslDistro", args.wsl_distro,
            "-MessageFile", win_message, "-ExpectedMessageSha256", digest,
        ]
        if args.probe:
            command.append("-ProbeOnly")
        env = dict(os.environ)
        # A Linux PowerShell module path is not a Windows module path.
        for key in list(env):
            if key.upper() == "PSMODULEPATH":
                del env[key]
        started_at_ms = int(time.time() * 1000)
        timeout = remaining(deadline, 16)
        try:
            call = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                env=env, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # subprocess.run includes process creation, communication and wait.
            # An OSError can occur after the child already invoked Submit.
            # Neither that error nor a timeout proves that nothing was sent.
            raise UiSendError(
                "ui_process_unavailable", phase="outcome_unknown",
                outcome="unknown", submit_attempted=None,
                last_confirmed_phase="ui_dispatched", cause=type(exc).__name__,
                enqueue_started_at_ms=started_at_ms,
            ) from exc
        result = parse_ui_result(call, digest, args.probe)
        stamp = result.get("submit_started_at_ms")
        if (result.get("phase") == "ui_invoked"
                and (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                     or not math.isfinite(stamp) or not started_at_ms <= stamp <= int(time.time() * 1000))):
            return {**result, "phase": "outcome_unknown", "outcome": "unknown",
                    "reason": "submit_time_unverifiable",
                    "enqueue_started_at_ms": started_at_ms}
        if result.get("phase") != "ui_invoked":
            stamp = started_at_ms
        result["enqueue_started_at_ms"] = stamp
        return result


def validate_input(args):
    try:
        import uuid
        args.thread = str(uuid.UUID(args.thread))
    except (ValueError, TypeError, AttributeError) as exc:
        raise UiSendError("invalid_thread") from exc
    if not args.title.strip() or not args.expect_cwd.strip():
        raise UiSendError("target_required")
    if not math.isfinite(args.wait) or not 0 <= args.wait <= 60:
        raise UiSendError("wait_out_of_range")
    actual_distro = os.environ.get("WSL_DISTRO_NAME")
    if not actual_distro or (args.wsl_distro and args.wsl_distro != actual_distro):
        raise UiSendError("wsl_context_unavailable")
    args.wsl_distro = actual_distro
    args.codex_home = Path(args.codex_home).expanduser().resolve()
    # The UI cannot prove which custom extension profile owns a displayed label.
    if args.codex_home != (Path.home() / ".codex").resolve():
        raise UiSendError("custom_codex_home_not_verified_for_ui")
    args.adapter = Path(args.adapter).expanduser().resolve()
    args.ui_script = Path(args.ui_script).expanduser().resolve()
    if not args.adapter.is_file() or not args.ui_script.is_file():
        raise UiSendError("sender_files_missing")
    path = Path(args.message_file)
    if not path.is_file() or path.is_symlink():
        raise UiSendError("message_missing_or_symlink")
    with path.open("rb") as stream:
        raw = stream.read(MAX_MESSAGE_BYTES + 1)
    if not 0 < len(raw) <= MAX_MESSAGE_BYTES:
        raise UiSendError("message_size")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise UiSendError("message_not_utf8") from exc
    if not text.strip() or "\0" in text:
        raise UiSendError("message_empty_or_nul")
    return raw, text


def run(args):
    raw, text = validate_input(args)
    digest = hashlib.sha256(raw).hexdigest()
    # Leaves a margin for the MSI wrapper's existing wait+30 second bound.
    deadline = time.monotonic() + args.wait + 24
    metadata = dict(thread_id=args.thread, message_sha256=digest)
    ui_dispatch_requested = False
    result = None
    try:
        with local_sender_lock():
            adapter = load_adapter(args.adapter)
            check_unique_target(adapter, args.codex_home, args.thread, args.title, args.expect_cwd)
            before = snapshot(args, deadline)
            check_snapshot_ready(before, args)
            baseline = {turn.get("id") for turn in before["turns"] if isinstance(turn, dict)}
            check_unique_target(adapter, args.codex_home, args.thread, args.title, args.expect_cwd)
            ui_dispatch_requested = True
            result = invoke_ui(args, raw, digest, deadline)
            if result.get("phase") != "ui_invoked":
                return {**metadata, **result, "retry_safe": False}
        # Observation is read-only and must not hold the desktop input lock.
        wait_deadline = min(deadline, time.monotonic() + args.wait)
        observation_error = None
        while time.monotonic() < wait_deadline:
            try:
                after = snapshot(args, wait_deadline)
                matched = correlate_turn(after, baseline, text, result["enqueue_started_at_ms"])
                if matched:
                    return {**metadata, **result, **matched}
            except UiSendError as exc:
                observation_error = exc.result["error_code"]
                break
            pause = min(0.25, wait_deadline - time.monotonic())
            if pause > 0:
                time.sleep(pause)
        return {
            **metadata, **result, "phase": "ui_invoked_unobserved",
            "outcome": "unknown", "observation_error": observation_error,
            "retry_safe": False,
        }
    except UiSendError as exc:
        return {**metadata, **exc.result}
    except Exception as exc:
        # A local failure before dispatch is known not to have sent anything.
        # After dispatch, preserve uncertainty and any phase already observed.
        return {
            **metadata,
            "phase": "outcome_unknown" if ui_dispatch_requested else "failed_before_send",
            "outcome": "unknown" if ui_dispatch_requested else "not_sent",
            "reason": type(exc).__name__,
            "submit_attempted": (result or {}).get("submit_attempted") if ui_dispatch_requested else False,
            "last_confirmed_phase": (result or {}).get("last_confirmed_phase", "ui_dispatched" if ui_dispatch_requested else "preflight"),
            "retry_safe": False,
        }


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--thread", required=True)
    value.add_argument("--title", required=True)
    value.add_argument("--expect-cwd", required=True)
    value.add_argument("--message-file", required=True)
    value.add_argument("--wait", type=float, default=15,
                       help="Read-only delivery observation in seconds (0..60)")
    value.add_argument("--probe", action="store_true", help="Select/check the existing UI only; never enter or send text")
    value.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    value.add_argument("--adapter", default=str(SCRIPT_DIR / "codex_connect.py"))
    value.add_argument("--ui-script", default=str(SCRIPT_DIR / "send_ui.ps1"))
    value.add_argument("--powershell", default=os.environ.get("CODEX_CONNECT_POWERSHELL"))
    value.add_argument("--wsl-distro", help="Must equal this process's WSL_DISTRO_NAME")
    return value


def main(argv=None):
    try:
        result = run(parser().parse_args(argv))
    except UiSendError as exc:
        result = exc.result
    except (OSError, ValueError) as exc:
        result = dict(phase="failed_before_send", outcome="not_sent",
                      reason=type(exc).__name__, submit_attempted=False,
                      last_confirmed_phase="preflight", retry_safe=False)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("phase") in ("observed", "ui_ready"):
        return 0
    return 3 if result.get("outcome") == "unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())
