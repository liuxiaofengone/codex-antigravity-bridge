"""Structured AGY result and permission-detail regression tests."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from codex_agy_bridge import agy_runner, server
from codex_agy_bridge.agy_jobs import AgyJobRegistry
from codex_agy_bridge.agy_result import (
    agy_result_error_kind,
    describe_agy_failure,
    failure_details,
    parse_agy_result,
)


def _envelope(**overrides):
    payload = {
        "conversation_id": "conversation-123",
        "status": "SUCCESS",
        "response": "",
        "denied_actions": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_permission_envelope_preserves_specific_blocking_evidence() -> None:
    stdout = _envelope(
        workdir="C:\\repo",
        denied_actions=[
            {
                "action": "RunCommand",
                "command": "git status --short",
                "resource": "command(git status --short)",
            }
        ],
    )

    result = parse_agy_result(
        stdout,
        "headless mode cannot prompt for permission",
        exit_code=0,
        output_format="json",
    )
    details = failure_details(result)

    assert agy_result_error_kind(result) == "permission"
    assert result.conversation_id == "conversation-123"
    assert result.cli_status == "SUCCESS"
    assert result.failed_command == "git status --short"
    assert result.permission_resource == "command(git status --short)"
    assert result.workdir == "C:\\repo"
    assert details["reason"] == "permission_blocked"
    assert details["retryable"] is False
    assert details["denied_actions"][0]["command"] == "git status --short"


def test_nested_result_event_is_supported() -> None:
    stdout = json.dumps({
        "event": "result",
        "result": json.loads(_envelope(
            denied_actions=[{"action": "RunCommand", "command": "rg --files"}],
        )),
    })

    result = parse_agy_result(
        stdout, "", exit_code=0, output_format="stream-json"
    )

    assert result.conversation_id == "conversation-123"
    assert result.failed_command == "rg --files"
    assert agy_result_error_kind(result) == "permission"


def test_missing_command_is_null_and_never_inferred_from_stderr() -> None:
    result = parse_agy_result(
        _envelope(denied_actions=[{"action": "RunCommand"}]),
        "The model suggested cnb status but no command metadata was supplied.",
        exit_code=0,
        output_format="json",
    )

    assert result.failed_command is None
    assert failure_details(result)["failed_command"] is None


def test_stderr_noise_and_ansi_do_not_discard_json_evidence() -> None:
    stdout = _envelope(
        denied_actions=[{"action": "RunCommand", "command": "git rev-parse HEAD"}]
    )
    stderr = "\x1b[31mpermission denied for a different retry\x1b[0m\nnoise"

    result = parse_agy_result(
        stdout, stderr, exit_code=0, output_format="json"
    )

    assert result.conversation_id == "conversation-123"
    assert result.failed_command == "git rev-parse HEAD"
    assert result.stderr == "permission denied for a different retry\nnoise"


def test_business_json_with_status_and_conversation_is_success() -> None:
    stdout = json.dumps({
        "conversation_id": "business-1",
        "status": "active",
        "response": "data",
    })

    result = parse_agy_result(
        stdout, "", exit_code=0, output_format="json"
    )

    assert result.conversation_id is None
    assert result.cli_status is None
    assert agy_result_error_kind(result) is None


def test_success_envelope_preserves_chinese_response() -> None:
    result = parse_agy_result(
        _envelope(response="检查完成"), "", exit_code=0, output_format="json"
    )

    assert result.text == _envelope(response="检查完成")
    assert agy_result_error_kind(result) is None
    assert result.reason is None


@pytest.mark.parametrize(
    "status",
    ["INVALID", "WAITING", "RUNNING", "CANCELLED", "INTERRUPTED", "TIMEOUT"],
)
def test_nonterminal_or_failed_cli_status_is_not_success(status: str) -> None:
    result = parse_agy_result(
        _envelope(status=status, response="partial"),
        "",
        exit_code=0,
        output_format="json",
    )

    assert result.cli_status == status
    assert agy_result_error_kind(result) == "unknown"
    assert result.reason == f"cli_status_{status.lower()}"


def test_finished_reason_marks_a_partial_success_envelope_as_failure() -> None:
    result = parse_agy_result(
        _envelope(
            response="partial response",
            finished_reason="print_timeout",
        ),
        "",
        exit_code=3,
        output_format="json",
    )

    assert result.finished_reason == "print_timeout"
    assert agy_result_error_kind(result) == "unknown"
    assert result.reason == "finished_reason_print_timeout"
    assert result.retryable is True


def test_permission_failure_details_are_redacted() -> None:
    result = parse_agy_result(
        _envelope(denied_actions=[{
            "action": "RunCommand",
            "command": "curl -H 'Authorization: Bearer super-secret-token' example.test",
            "token": "another-secret",
        }]),
        "",
        exit_code=0,
        output_format="json",
    )

    rendered = json.dumps(failure_details(result), ensure_ascii=False)
    message = describe_agy_failure(result)

    assert "another-secret" not in rendered
    assert "super-secret-token" not in rendered
    assert "super-secret-token" not in message
    assert "REDACTED" in rendered
    assert message.startswith("AGY_PERMISSION_DENIED:")
    assert "Details:" in message


def test_plain_permission_stderr_keeps_legacy_error_and_adds_details() -> None:
    result = parse_agy_result(
        "",
        "user denied permission to run command: git status --short",
        exit_code=0,
        output_format=None,
    )

    assert agy_result_error_kind(result) == "permission"
    assert result.reason == "permission_blocked"
    assert result.retryable is False
    assert describe_agy_failure(result).startswith("AGY_PERMISSION_DENIED:")


def test_runner_attaches_parsed_details_without_pty_retry(monkeypatch) -> None:
    payload = _envelope(denied_actions=[{
        "action": "RunCommand",
        "command": "git status --short",
    }])
    monkeypatch.setattr(agy_runner, "find_agy", lambda: "agy")
    monkeypatch.setattr(agy_runner, "resolve_agy_environment", lambda force=False: {})
    monkeypatch.setattr(
        agy_runner,
        "_run_subprocess",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, payload, ""),
    )
    monkeypatch.setattr(
        agy_runner,
        "_run_with_pty",
        lambda *args, **kwargs: pytest.fail("permission denial must not retry"),
    )

    result = agy_runner.run_agy("Inspect repository", output_format="json")

    assert result.conversation_id == "conversation-123"
    assert result.failed_command == "git status --short"
    assert agy_result_error_kind(result) == "permission"


def test_async_failure_propagates_structured_details(monkeypatch) -> None:
    payload = _envelope(denied_actions=[{
        "action": "RunCommand",
        "command": "git rev-parse HEAD",
        "resource": "command(git rev-parse HEAD)",
    }])
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.run_agy",
        lambda *args, **kwargs: parse_agy_result(
            payload, "", exit_code=0, output_format="json"
        ),
    )
    with AgyJobRegistry() as registry:
        job_id = registry.start("Inspect repository", output_format="json")
        deadline = time.monotonic() + 2
        while True:
            status = registry.status(job_id)
            if status["state"] not in {"queued", "running"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert status["state"] == "failed"
    assert status["error_kind"] == "permission"
    assert status["reason"] == "permission_blocked"
    assert status["conversation_id"] == "conversation-123"
    assert status["failed_command"] == "git rev-parse HEAD"
    assert status["retryable"] is False


def test_sync_server_error_keeps_prefix_and_adds_redacted_json(monkeypatch) -> None:
    result = parse_agy_result(
        _envelope(denied_actions=[{
            "action": "RunCommand",
            "command": "git status --short",
            "token": "must-not-leak",
        }]),
        "",
        exit_code=0,
        output_format="json",
    )
    monkeypatch.setattr(server, "run_agy", lambda *args, **kwargs: result)

    with pytest.raises(RuntimeError) as error:
        server.agy_ask("Inspect repository")

    message = str(error.value)
    assert message.startswith("AGY_PERMISSION_DENIED:")
    assert '"reason": "permission_blocked"' in message
    assert '"failed_command": "git status --short"' in message
    assert "must-not-leak" not in message
