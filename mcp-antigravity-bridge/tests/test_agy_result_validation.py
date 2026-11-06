"""Regression coverage for soft-denied AGY calls with process exit code zero."""

import json
import subprocess
import sys
import time

import pytest

from codex_agy_bridge import agy_runner, server
from codex_agy_bridge.agy_jobs import AgyJobRegistry
from codex_agy_bridge.agy_result import AgyResult


def envelope(response="", denied_actions=None, status="SUCCESS", **extra):
    return json.dumps({
        "conversation_id": "test-conversation",
        "status": status,
        "response": response,
        "denied_actions": denied_actions or [],
        **extra,
    })


@pytest.mark.parametrize("tool", [server.agy_ask, server.agy_ask_json])
@pytest.mark.parametrize("denied", [["RunCommand"], [{"action": "RunCommand"}]])
def test_zero_exit_with_denied_actions_is_permission_failure(monkeypatch, tool, denied):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(
        text=envelope(denied_actions=denied), exit_code=0,
    ))
    with pytest.raises(RuntimeError, match="AGY_PERMISSION_DENIED"):
        tool("Inspect repository")


@pytest.mark.parametrize("text", ["", "  \n", envelope(), envelope(response=" \n")])
def test_zero_exit_with_empty_answer_is_failure(monkeypatch, text):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(text, 0))
    with pytest.raises(RuntimeError, match="AGY_EMPTY_OUTPUT"):
        server.agy_ask("Inspect repository")


def test_json_failure_status_cannot_become_success(monkeypatch):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(
        envelope(status="ERROR", error="agent failed"), 0,
    ))
    with pytest.raises(RuntimeError, match="AGY_FAILED"):
        server.agy_ask_json("Inspect repository")


@pytest.mark.parametrize("response", ["DONE", ""])
def test_stderr_permission_denial_is_failure_even_with_zero_exit(monkeypatch, response):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(
        response, 0, stderr="user denied permission to run command: git status --short",
    ))
    with pytest.raises(RuntimeError, match="AGY_PERMISSION_DENIED"):
        server.agy_ask("Inspect repository")


@pytest.mark.parametrize("text", [
    "DONE", '{"ok": true}',
    envelope(response="The message 'user denied permission to run command' is diagnostic text."),
])
def test_usable_answers_are_preserved(monkeypatch, text):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(text, 0))
    assert server.agy_ask("Explain diagnostics") == text


@pytest.mark.parametrize("diagnostic", [
    "user denied permission to run command: git status --short",
    'jetski: no output produced - a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied.',
])
def test_permission_denial_does_not_trigger_pty_retry(monkeypatch, diagnostic):
    calls = []
    monkeypatch.setattr(agy_runner, "find_agy", lambda: "agy")
    monkeypatch.setattr(agy_runner, "resolve_agy_environment", lambda force=False: {})
    monkeypatch.setattr(agy_runner, "_run_subprocess", lambda *a, **kw:
        subprocess.CompletedProcess([], 0, "", diagnostic))
    monkeypatch.setattr(agy_runner, "_run_with_pty", lambda *a, **kw:
        calls.append("unexpected retry") or ("RETRIED", 0))
    result = agy_runner.run_agy("Inspect repository")
    assert calls == []
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: result)
    with pytest.raises(RuntimeError, match="AGY_PERMISSION_DENIED"):
        server.agy_ask("Inspect repository")


@pytest.mark.parametrize("text,kind", [
    (envelope(denied_actions=["RunCommand"]), "permission"),
    (envelope(), "empty_output"),
])
def test_async_jobs_do_not_report_soft_failures_as_completed(monkeypatch, text, kind):
    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", lambda *a, **kw: AgyResult(text, 0))
    with AgyJobRegistry() as registry:
        job = registry.start("Inspect repository")
        deadline = time.monotonic() + 2
        while True:
            status = registry.status(job)
            if status["state"] not in {"queued", "running"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert status["state"] == "failed"
        assert status["error_kind"] == kind
        assert status["exit_code"] == 0


@pytest.mark.parametrize("text", [
    '{"status":"active","conversation_id":"business-session"}',
    '{"status":"SUCCESS","conversation_id":"business-session"}',
    '{"status":[],"conversation_id":"business-session","response":"data"}',
])
def test_business_json_with_status_and_session_fields_remains_usable(monkeypatch, text):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(text, 0))
    assert server.agy_ask("Return business data") == text


@pytest.mark.parametrize("text", [
    json.dumps({"event":"result", "result":json.loads(envelope(denied_actions=["RunCommand"]))}),
    json.dumps({"conversation_id":"test-conversation", "result":"SUCCESS", "response":"", "denied_actions":["RunCommand"]}),
])
def test_cli_envelope_variants_preserve_permission_failure(monkeypatch, text):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(text, 0))
    with pytest.raises(RuntimeError, match="AGY_PERMISSION_DENIED"):
        server.agy_ask_json("Inspect repository")

def test_cli_utf8_output_survives_windows_legacy_default_decoder(monkeypatch):
    # AGY emits UTF-8 even when the Windows parent process defaults to GBK.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp936")
    code = "import sys; sys.stdout.buffer.write(bytes.fromhex('e4b8ade69687')); sys.stderr.buffer.write(bytes.fromhex('e6b58be8af95'))"
    result = agy_runner._run_subprocess([sys.executable, "-c", code], None, 10)
    assert result.returncode == 0
    assert result.stdout == "中文"
    assert result.stderr == "测试"


def test_cli_invalid_output_bytes_do_not_lose_the_entire_capture(monkeypatch):
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp936")
    code = "import sys; sys.stdout.buffer.write(b'OK\\xff')"
    result = agy_runner._run_subprocess([sys.executable, "-c", code], None, 10)
    assert result.returncode == 0
    assert result.stdout == "OK\ufffd"


@pytest.mark.parametrize("status", ["INVALID", "WAITING", "RUNNING"])
def test_nonterminal_or_invalid_cli_status_is_failure(monkeypatch, status):
    monkeypatch.setattr(server, "run_agy", lambda *a, **kw: AgyResult(
        envelope(status=status, response="Incomplete"), 0,
    ))
    with pytest.raises(RuntimeError, match="AGY_FAILED"):
        server.agy_ask_json("Inspect repository")
