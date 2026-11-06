"""Parse AGY output into stable, redacted execution evidence."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

_ANSI = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_AUTHORIZATION = re.compile(r"(?i)\b(authorization\s*:\s*)[^\r\n]+")
_SECRET_KEY = re.compile(
    r"(?i)(?:^|_)(?:token|secret|password|authorization|api_?key|cookie)(?:$|_)"
)
_PERMISSION_MARKERS = (
    "user denied permission",
    "permission check failed",
    "tool call was denied",
    "tool call is denied",
    "soft-denied",
    "auto-denied",
    "permission denied for",
)
_NETWORK_MARKERS = (
    "dial tcp",
    "connectex",
    "connection refused",
    "connection reset",
    "connection timed out",
    "network is unreachable",
    "no such host",
    "proxyconnect",
    "failed to fetch",
    "network error",
)
_AUTH_MARKERS = (
    "authentication required",
    "authentication failed",
    "login required",
    "login to continue",
    "invalid token",
    "invalid_token",
    "invalid_grant",
    "authorization failed",
    "unauthenticated",
    "oauth token",
)
_CLI_STATUSES = {
    "SUCCESS",
    "ERROR",
    "FAILED",
    "FAILURE",
    "CANCELLED",
    "CANCELED",
    "INTERRUPTED",
    "TIMEOUT",
    "ABORTED",
    "INVALID",
    "WAITING",
    "RUNNING",
}


@dataclass
class AgyResult:
    text: str
    exit_code: int
    used_pty: bool = False
    stderr: str = ""
    conversation_id: str | None = None
    cli_status: str | None = None
    denied_actions: list[Any] | None = None
    failed_command: str | None = None
    permission_resource: str | None = None
    workdir: str | None = None
    capability_id: str | None = None
    retryable: bool | None = None
    remediation: str | None = None
    reason: str | None = None
    evidence: str | None = None
    output_format: str | None = None
    finished_reason: str | None = None


def _clean(raw: str | None) -> str:
    if not raw:
        return ""
    return _ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "\n").strip()


def _redact_text(value: str) -> str:
    value = _BEARER.sub(r"\1[REDACTED]", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    if isinstance(parsed, (dict, list)):
        return json.dumps(_redact(parsed), ensure_ascii=False)
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    return value


def _payload_candidate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("event") == "result":
        value = value.get("result")
    if not isinstance(value, dict):
        return None
    status = value.get("status", value.get("result"))
    if not isinstance(status, str) or status.upper() not in _CLI_STATUSES:
        return None
    if not any(
        key in value
        for key in (
            "response",
            "error",
            "denied_actions",
        )
    ):
        return None
    return value


def _extract_payload(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return _payload_candidate(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        pass
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        try:
            candidate = _payload_candidate(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
        if candidate is not None:
            return candidate
    return None


def _classify(detail: str) -> str:
    lowered = detail.lower()
    if any(marker in lowered for marker in _PERMISSION_MARKERS):
        return "permission"
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return "network"
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return "authentication"
    return "unknown"


def _command_from_action(action: Any) -> tuple[str | None, str | None]:
    if not isinstance(action, dict):
        return None, None
    command = action.get("command", action.get("failed_command"))
    if not isinstance(command, str) or not command.strip():
        arguments = action.get("arguments")
        command = arguments.get("command") if isinstance(arguments, dict) else None
    resource = action.get(
        "resource",
        action.get("permission_resource", action.get("permission")),
    )
    command_value = command.strip() if isinstance(command, str) and command.strip() else None
    resource_value = resource.strip() if isinstance(resource, str) and resource.strip() else None
    return command_value, resource_value


def _failure_from_text(text: str) -> tuple[str | None, str | None, bool | None]:
    kind = _classify(text)
    if kind == "permission":
        return kind, "permission_blocked", False
    if kind == "network":
        return kind, "network_error", True
    if kind == "authentication":
        return kind, "authentication_required", False
    return None, None, None


def parse_agy_result(
    stdout: str,
    stderr: str,
    exit_code: int,
    *,
    output_format: str | None,
    used_pty: bool = False,
    workdir: str | None = None,
    capability_id: str | None = None,
) -> AgyResult:
    """Parse cleaned CLI output while retaining specific failure evidence."""
    cleaned_stdout = _clean(stdout)
    cleaned_stderr = _clean(stderr)
    payload = _extract_payload(cleaned_stdout)
    display_text = cleaned_stdout or cleaned_stderr or "agy returned no diagnostic output"
    result = AgyResult(
        text=_redact_text(display_text),
        exit_code=exit_code,
        used_pty=used_pty,
        stderr=_redact_text(cleaned_stderr),
        output_format=output_format,
        workdir=workdir,
        capability_id=capability_id,
    )
    if payload is None:
        kind, reason, retryable = _failure_from_text(
            f"{cleaned_stdout}\n{cleaned_stderr}"
        )
        if exit_code != 0:
            kind = kind or "unknown"
            reason = reason or f"exit_code_{exit_code}"
        elif not cleaned_stdout.strip():
            kind = kind or "empty_output"
            reason = reason or "empty_output"
            retryable = True if retryable is None else retryable
        result.reason = reason
        result.retryable = retryable
        result.evidence = "unavailable" if payload is None else "cli_tool_result"
        if kind == "permission":
            result.remediation = (
                "Review the command interactively or authorize the specific command "
                "for this task before retrying."
            )
        elif kind == "authentication":
            result.remediation = "Complete AGY sign-in before retrying."
        elif kind == "network":
            result.remediation = "Check the proxy or network connection before retrying."
        return result

    status_value = payload.get("status", payload.get("result"))
    status = str(status_value).upper() if isinstance(status_value, str) else None
    denied_actions = payload.get("denied_actions")
    if not isinstance(denied_actions, list):
        denied_actions = None
    conversation_id = payload.get("conversation_id")
    finished_reason = payload.get("finished_reason")
    result.conversation_id = (
        conversation_id if isinstance(conversation_id, str) else None
    )
    result.cli_status = status
    result.denied_actions = _redact(denied_actions)
    result.finished_reason = (
        finished_reason if isinstance(finished_reason, str) else None
    )
    payload_workdir = payload.get("workdir")
    if isinstance(payload_workdir, str) and payload_workdir.strip():
        result.workdir = payload_workdir.strip()
    result.evidence = "cli_tool_result"

    direct_command = payload.get("failed_command")
    result.failed_command = (
        direct_command.strip()
        if isinstance(direct_command, str) and direct_command.strip()
        else None
    )
    direct_resource = payload.get("permission_resource")
    result.permission_resource = (
        direct_resource.strip()
        if isinstance(direct_resource, str) and direct_resource.strip()
        else None
    )
    for action in denied_actions or []:
        command, resource = _command_from_action(action)
        if result.failed_command is None and command is not None:
            result.failed_command = command
        if result.permission_resource is None and resource is not None:
            result.permission_resource = resource

    if denied_actions:
        result.reason = "permission_blocked"
        result.retryable = False
        result.remediation = (
            "Review the command interactively or authorize the specific command "
            "for this task before retrying."
        )
        return result

    combined = cleaned_stderr
    text_kind, text_reason, text_retryable = _failure_from_text(combined)
    if text_kind == "permission":
        result.reason = text_reason
        result.retryable = text_retryable
        result.remediation = (
            "Review the command interactively or authorize the specific command "
            "for this task before retrying."
        )
        return result
    if result.finished_reason:
        result.reason = f"finished_reason_{result.finished_reason.lower()}"
        result.retryable = result.finished_reason.lower() not in {"interrupted"}
        return result
    if exit_code != 0:
        result.reason = text_reason or f"exit_code_{exit_code}"
        result.retryable = text_retryable
        return result
    if status != "SUCCESS":
        result.reason = f"cli_status_{status.lower() if status else 'unknown'}"
        result.retryable = True
        return result
    error = payload.get("error")
    if error:
        kind, reason, retryable = _failure_from_text(str(error))
        result.reason = reason or "agent_error"
        result.retryable = retryable
        return result
    response = payload.get("response")
    if not isinstance(response, str) or not response.strip():
        result.reason = "empty_output"
        result.retryable = True
        return result
    return result


def agy_result_error_kind(result: AgyResult) -> str | None:
    """Return the stable failure kind, including CLI soft failures."""
    if result.reason is None:
        parsed = parse_agy_result(
            result.text,
            result.stderr,
            result.exit_code,
            output_format=result.output_format,
            used_pty=result.used_pty,
            workdir=result.workdir,
            capability_id=result.capability_id,
        )
        if parsed.reason is None:
            return None
        reason = parsed.reason
    else:
        reason = result.reason
    if reason == "permission_blocked":
        return "permission"
    if reason in {"network_error", "authentication_required", "empty_output"}:
        return reason.replace("_error", "").replace("_required", "").replace(
            "_output", "_output"
        )
    if reason and reason.startswith("cli_status_"):
        return "unknown"
    if reason and reason.startswith("exit_code_"):
        kind, _, _ = _failure_from_text(f"{result.text}\n{result.stderr}")
        return kind or "unknown"
    if reason:
        return "unknown"
    return None


def _result_dict(result: AgyResult) -> dict[str, Any]:
    return asdict(result)


def failure_details(result: AgyResult) -> dict[str, Any]:
    """Return the stable, redacted machine-readable failure details."""
    parsed = result
    if result.reason is None:
        parsed = parse_agy_result(
            result.text,
            result.stderr,
            result.exit_code,
            output_format=result.output_format,
            used_pty=result.used_pty,
            workdir=result.workdir,
            capability_id=result.capability_id,
        )
    data = _result_dict(result)
    parsed_data = _result_dict(parsed)
    for key, value in parsed_data.items():
        if data.get(key) is None and value is not None:
            data[key] = value
    fields = (
        "error_kind",
        "reason",
        "retryable",
        "conversation_id",
        "cli_status",
        "denied_actions",
        "failed_command",
        "permission_resource",
        "workdir",
        "capability_id",
        "remediation",
        "evidence",
        "finished_reason",
        "exit_code",
    )
    data["error_kind"] = agy_result_error_kind(result)
    return {field: _redact(data.get(field)) for field in fields}


def describe_agy_failure(result: AgyResult) -> str:
    detail = result.stderr or result.text or "agy returned no diagnostic output"
    kind = agy_result_error_kind(result)
    details = json.dumps(
        failure_details(result),
        ensure_ascii=False,
        sort_keys=True,
    )
    if kind == "permission":
        return (
            "AGY_PERMISSION_DENIED: agy could not obtain tool approval in headless mode. "
            "Review the requested action interactively in the same workspace, or explicitly "
            "authorize a narrow permissions.allow rule before retrying. "
            f"Original error: {_redact_text(detail)} Details: {details}"
        )
    if kind == "empty_output":
        return (
            "AGY_EMPTY_OUTPUT: agy returned no usable response. "
            "A zero exit code or CLI SUCCESS alone does not confirm task completion. "
            f"Original output: {_redact_text(detail)} Details: {details}"
        )
    if kind == "network":
        return (
            "AGY_PROXY_ERROR: agy could not reach its network endpoint. "
            "Check the local proxy or TUN mode before retrying. "
            f"Original error: {_redact_text(detail)} Details: {details}"
        )
    if kind == "authentication":
        return (
            "AGY_LOGIN_REQUIRED: agy authentication is required. "
            "Run `agy` interactively through the working proxy, complete login, "
            "then tell Codex to retry the task once. "
            f"Original error: {_redact_text(detail)} Details: {details}"
        )
    return (
        f"AGY_FAILED: agy exited with code {result.exit_code}: "
        f"{_redact_text(detail)} Details: {details}"
    )
