"""Static and explicitly active preflight checks for AGY task contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping

from .agy_capabilities import CapabilityDiscoveryError, inspect_agy_capabilities
from .agy_evidence import PolicyEvidenceError, load_verified_policy_evidence
from .agy_policy import (
    ExecutionOptions,
    PolicyError,
    build_session_args,
    load_inherited_permissions,
    parse_execution_options,
)

_TARGETS = {"local", "remote_ssh"}
_MODES = {"inherit", "plan", "accept-edits", "auto"}
_REMOTE_REQUIRED = {
    "workspace_id",
    "repository",
    "ssh_alias",
    "worktree",
    "branch",
    "baseline_sha",
    "target_kind",
    "owned_paths",
    "forbidden_paths",
    "evidence_refs",
    "identity",
}
_HEX_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_PREFLIGHT_TTL = timedelta(minutes=5)
_PREFLIGHTS: dict[str, tuple[dict[str, Any], "TaskContract"]] = {}
_PREFLIGHTS_LOCK = RLock()


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    capability_id: str
    execution_target: str
    local_dir: str
    mode: str
    approved_commands: tuple[str, ...]
    authorization_ref: str | None
    owned_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    preflight_commands: tuple[str, ...]
    requires_hard_tool_interception: bool = False
    remote_context: dict[str, Any] | None = None
    caller_evidence_refs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | "TaskContract") -> "TaskContract":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, Mapping):
            raise ValueError("contract must be an object")
        required = {
            "task_id",
            "capability_id",
            "execution_target",
            "local_dir",
            "mode",
            "approved_commands",
            "preflight_commands",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise ValueError(f"contract is missing: {', '.join(missing)}")
        target = raw["execution_target"]
        if target not in _TARGETS:
            raise ValueError("execution_target must be local or remote_ssh")
        mode = raw["mode"]
        if mode not in _MODES:
            raise ValueError("contract mode is invalid")
        authorization_ref = raw.get("authorization_ref")
        if authorization_ref is not None and (
            not isinstance(authorization_ref, str) or not authorization_ref.strip()
        ):
            raise ValueError("authorization_ref must be a non-empty string")
        requires_hard = raw.get("requires_hard_tool_interception", False)
        if not isinstance(requires_hard, bool):
            raise ValueError("requires_hard_tool_interception must be a boolean")
        remote_context = raw.get("remote_context")
        if remote_context is not None and not isinstance(remote_context, Mapping):
            raise ValueError("remote_context must be an object")
        context_map = dict(remote_context) if remote_context is not None else {}
        return cls(
            task_id=_required_string(raw, "task_id"),
            capability_id=_required_string(raw, "capability_id"),
            execution_target=str(target),
            local_dir=_required_string(raw, "local_dir"),
            mode=str(mode),
            approved_commands=_string_list(raw["approved_commands"], "approved_commands"),
            authorization_ref=(
                authorization_ref.strip() if isinstance(authorization_ref, str) else None
            ),
            owned_paths=_path_list(
                raw.get("owned_paths", context_map.get("owned_paths", [])),
                "owned_paths",
            ),
            forbidden_paths=_path_list(
                raw.get(
                    "forbidden_paths",
                    context_map.get("forbidden_paths", []),
                ),
                "forbidden_paths",
            ),
            preflight_commands=_string_list(
                raw["preflight_commands"], "preflight_commands"
            ),
            requires_hard_tool_interception=requires_hard,
            remote_context=context_map if remote_context is not None else None,
            caller_evidence_refs=_string_list(
                raw.get("caller_evidence_refs", []), "caller_evidence_refs"
            ),
        )

    def digest(self) -> str:
        encoded = json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must contain non-empty strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _path_list(value: Any, name: str) -> tuple[str, ...]:
    values = _string_list(value, name)
    for item in values:
        normalized = item.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError(f"{name} must contain relative paths")
        if normalized == "." or ".." in normalized.split("/"):
            raise ValueError(f"{name} paths must not escape the workspace")
    return values


def _remote_context_complete(context: Mapping[str, Any] | None) -> bool:
    if not context or not _REMOTE_REQUIRED.issubset(context):
        return False
    list_fields = {"owned_paths", "forbidden_paths", "evidence_refs"}
    if any(
        not isinstance(context.get(key), str) or not str(context[key]).strip()
        for key in _REMOTE_REQUIRED - list_fields - {"identity"}
    ):
        return False
    if not _HEX_SHA.fullmatch(str(context["baseline_sha"])):
        return False
    for key in list_fields:
        items = context.get(key)
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            return False
        if key == "evidence_refs" and not items:
            return False
    worktree = PurePosixPath(str(context["worktree"]))
    if not worktree.is_absolute() or ".." in worktree.parts:
        return False
    identity = context.get("identity")
    return isinstance(identity, Mapping)


def _remote_identity_problem(
    context: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    identity = context.get("identity")
    if not isinstance(identity, Mapping):
        return "remote_identity_not_read"
    keys = {
        "workspace_id",
        "repository",
        "worktree",
        "branch",
        "baseline_sha",
    }
    if any(identity.get(key) != context.get(key) for key in keys):
        return "remote_identity_changed"
    if identity.get("source") != "caller_verified":
        return "remote_identity_unverified"
    try:
        observed_at = datetime.fromisoformat(
            str(identity["observed_at"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError):
        return "remote_identity_stale"
    if observed_at.tzinfo is None:
        return "remote_identity_stale"
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = current - observed_at.astimezone(timezone.utc)
    if age < timedelta(seconds=-30) or age > _PREFLIGHT_TTL:
        return "remote_identity_stale"
    return None


def _report(
    contract: TaskContract,
    capability_id: str,
    policy_digest: str,
    state: str,
    checks: dict[str, str],
    blockers: list[str],
    enforcement: str,
    evidence_grade: str,
    now: datetime,
) -> dict[str, Any]:
    expires_at = now + _PREFLIGHT_TTL
    material = {
        "contract_digest": contract.digest(),
        "capability_id": capability_id,
        "policy_digest": policy_digest,
        "checks": checks,
        "expires_at": expires_at.isoformat(),
    }
    preflight_id = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "preflight_id": preflight_id,
        "contract_digest": material["contract_digest"],
        "capability_id": capability_id,
        "policy_digest": policy_digest,
        "checks": checks,
        "state": state,
        "expires_at": expires_at.isoformat(),
        "blockers": list(dict.fromkeys(blockers)),
        "enforcement": enforcement,
        "evidence_grade": evidence_grade,
    }


def remember_preflight(
    report: Mapping[str, Any],
    contract: TaskContract | Mapping[str, Any],
) -> None:
    parsed = TaskContract.from_mapping(contract)
    preflight_id = str(report.get("preflight_id", ""))
    if not preflight_id:
        raise ValueError("preflight report has no id")
    with _PREFLIGHTS_LOCK:
        _PREFLIGHTS[preflight_id] = (dict(report), parsed)


def load_preflight_entry(
    preflight_id: str,
) -> tuple[dict[str, Any], TaskContract] | None:
    with _PREFLIGHTS_LOCK:
        entry = _PREFLIGHTS.get(preflight_id)
    if entry is None:
        return None
    report, contract = entry
    try:
        expires_at = datetime.fromisoformat(str(report["expires_at"]))
    except (KeyError, ValueError):
        return entry
    if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
        return entry
    return entry


def clear_preflight_reports() -> None:
    with _PREFLIGHTS_LOCK:
        _PREFLIGHTS.clear()


def _remember_and_return(
    report: dict[str, Any],
    contract: TaskContract,
) -> dict[str, Any]:
    remember_preflight(report, contract)
    return report


def preflight(
    contract: TaskContract | Mapping[str, Any],
    active_probe: bool = False,
) -> dict[str, Any]:
    """Validate a task contract; model execution requires an explicit opt-in."""
    parsed = TaskContract.from_mapping(contract)
    if not isinstance(active_probe, bool):
        raise ValueError("active_probe must be a boolean")
    now = datetime.now(timezone.utc)
    checks: dict[str, str] = {}
    blockers: list[str] = []
    unknown: list[str] = []
    enforcement = "policy_and_prompt"
    evidence_grade = "unavailable"
    try:
        capability = inspect_agy_capabilities()
    except CapabilityDiscoveryError:
        checks["capability"] = "unknown"
        unknown.append("capability_unavailable")
        return _remember_and_return(
            _report(
                parsed,
                parsed.capability_id,
                "",
                "unknown",
                checks,
                unknown,
                enforcement,
                evidence_grade,
                now,
            ),
            parsed,
        )
    if capability.capability_id != parsed.capability_id:
        checks["capability"] = "blocked"
        blockers.append("capability_changed")
    else:
        checks["capability"] = "passed"

    commands = tuple(
        dict.fromkeys(parsed.approved_commands + parsed.preflight_commands)
    )
    if commands and not parsed.authorization_ref:
        checks["authorization"] = "blocked"
        blockers.append("authorization_ref_required")
    else:
        checks["authorization"] = "passed"
    if parsed.mode == "plan" and parsed.preflight_commands:
        checks["mode"] = "blocked"
        blockers.append("plan_mode_cannot_execute_preflight")
    else:
        checks["mode"] = "passed"

    if parsed.requires_hard_tool_interception:
        checks["enforcement"] = "blocked"
        blockers.append("hard_tool_interception_unsupported")
        enforcement = "unsupported_enforcement"
    else:
        checks["enforcement"] = "passed"

    local_dir = Path(parsed.local_dir).expanduser()
    if not local_dir.is_dir():
        checks["target"] = "blocked"
        blockers.append("local_dir_missing")
    elif parsed.execution_target == "remote_ssh":
        if not _remote_context_complete(parsed.remote_context):
            checks["target"] = "blocked"
            blockers.append("remote_context_identity_incomplete")
        else:
            identity_problem = _remote_identity_problem(
                parsed.remote_context,
                now=now,
            )
            if identity_problem == "remote_identity_changed":
                checks["target"] = "blocked"
                blockers.append(identity_problem)
            elif identity_problem:
                checks["target"] = "unknown"
                unknown.append(identity_problem)
            else:
                checks["target"] = "passed"
                checks["remote_identity"] = "caller_verified"
                evidence_grade = "caller_verified"
    else:
        checks["target"] = "passed"

    inherited: dict[str, Any] | None = None
    if commands:
        try:
            load_verified_policy_evidence(
                capability,
                parsed.mode,
                ("exact_command",),
            )
            inherited = load_inherited_permissions()
            checks["policy_evidence"] = "passed"
        except PolicyEvidenceError:
            checks["policy_evidence"] = "unknown"
            unknown.append("policy_unverified")
        except PolicyError:
            checks["policy_evidence"] = "blocked"
            blockers.append("manual_policy_required")
    else:
        checks["policy_evidence"] = "unknown"
        unknown.append("policy_unverified")

    if not parsed.preflight_commands:
        checks["preflight_commands"] = "unknown"
        unknown.append("no_preflight_commands")
    else:
        checks["preflight_commands"] = "pending"

    policy_digest = str(capability.settings_summary.get("digest", ""))
    state = "blocked" if blockers else ("unknown" if unknown else "ready_for_probe")
    report = _report(
        parsed,
        capability.capability_id,
        policy_digest,
        state,
        checks,
        blockers + unknown,
        enforcement,
        evidence_grade,
        now,
    )
    if not active_probe or state != "ready_for_probe":
        return _remember_and_return(report, parsed)
    if os.environ.get("AGY_PREFLIGHT_PROBE_OPT_IN") != "1":
        report["state"] = "blocked"
        report["blockers"].append("active_probe_opt_in_required")
        return _remember_and_return(report, parsed)
    assert inherited is not None
    observation = _run_active_probe(
        parsed,
        capability,
        inherited,
        timeout=60.0,
    )
    report["checks"].update(observation.get("checks", {}))
    report["blockers"] = list(dict.fromkeys(
        report["blockers"] + list(observation.get("blockers", []))
    ))
    report["evidence_grade"] = str(
        observation.get("evidence_grade", "unavailable")
    )
    observed_state = str(observation.get("state", "unknown"))
    if observed_state == "ready" and report["evidence_grade"] != "cli_tool_result":
        report["state"] = "unknown"
        report["blockers"].append("model_report_only")
    elif observed_state not in {"ready", "blocked", "unknown"}:
        report["state"] = "unknown"
        report["blockers"].append("invalid_active_probe_state")
    else:
        report["state"] = observed_state
    report["blockers"] = list(dict.fromkeys(report["blockers"]))
    return _remember_and_return(report, parsed)


def _command_from_tool_step(step: Mapping[str, Any]) -> str | None:
    info = step.get("tool_info")
    if not isinstance(info, Mapping):
        return None
    parameters = info.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    command = parameters.get("CommandLine", parameters.get("command"))
    return command.strip() if isinstance(command, str) and command.strip() else None


def _evaluate_active_output(
    commands: tuple[str, ...],
    output: str,
    *,
    exit_code: int = 0,
) -> dict[str, Any]:
    expected = set(commands)
    completed: set[str] = set()
    permission_error = False
    tool_error = False
    tool_events = 0
    result_event = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, Mapping):
            continue
        if event.get("event") == "result":
            result_event = True
            result_payload = event.get("result")
            if isinstance(result_payload, Mapping) and result_payload.get(
                "denied_actions"
            ):
                permission_error = True
        step = event.get("step_update")
        if not isinstance(step, Mapping) or step.get("step_type") != "tool":
            continue
        tool_events += 1
        command = _command_from_tool_step(step)
        if command not in expected:
            continue
        info = step.get("tool_info")
        error = info.get("error") if isinstance(info, Mapping) else None
        state = str(step.get("state", "")).upper()
        if error or state == "ERROR":
            error_text = str(error or state).lower()
            if any(
                marker in error_text
                for marker in (
                    "permission",
                    "denied",
                    "deny rule",
                    "auto-denied",
                )
            ):
                permission_error = True
            else:
                tool_error = True
        elif state == "DONE" and isinstance(info, Mapping) and "output" in info:
            completed.add(command)
    if permission_error:
        return {
            "state": "blocked",
            "checks": {"preflight_commands": "denied"},
            "blockers": ["permission_blocked"],
            "evidence_grade": "cli_tool_result",
        }
    if tool_error:
        return {
            "state": "blocked",
            "checks": {"preflight_commands": "tool_error"},
            "blockers": ["tool_error"],
            "evidence_grade": "cli_tool_result",
        }
    if exit_code != 0:
        return {
            "state": "blocked",
            "checks": {"preflight_commands": "failed"},
            "blockers": ["probe_failed"],
            "evidence_grade": (
                "cli_tool_result" if tool_events or result_event else "unavailable"
            ),
        }
    if expected and expected.issubset(completed):
        return {
            "state": "ready",
            "checks": {"preflight_commands": "passed"},
            "blockers": [],
            "evidence_grade": "cli_tool_result",
        }
    if tool_events:
        return {
            "state": "unknown",
            "checks": {"preflight_commands": "incomplete"},
            "blockers": ["preflight_tool_result_incomplete"],
            "evidence_grade": "cli_tool_result",
        }
    return {
        "state": "unknown",
        "checks": {"preflight_commands": "unavailable"},
        "blockers": ["model_report_only" if result_event else "no_cli_tool_result"],
        "evidence_grade": "agent_report_only" if result_event else "unavailable",
    }


def _run_active_probe(
    contract: TaskContract,
    capability: Any,
    inherited_permissions: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    options = parse_execution_options({
        "mode": contract.mode,
        "approved_commands": list(
            dict.fromkeys(contract.approved_commands + contract.preflight_commands)
        ),
        "authorization_ref": contract.authorization_ref,
    })
    session_args = build_session_args(
        options, capability, inherited_permissions
    )
    from .agy_runner import find_agy

    binary = find_agy()
    if not binary:
        return {
            "state": "blocked",
            "checks": {"preflight_commands": "unavailable"},
            "blockers": ["agy_binary_missing"],
            "evidence_grade": "unavailable",
        }
    prompt = "\n".join([
        "Run only the following preflight commands, in order:",
        *[f"- {command}" for command in contract.preflight_commands],
        "Stop immediately after the first permission denial or error.",
        "Do not implement the task and do not run any other command.",
    ])
    command = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        *session_args,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=contract.local_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return {
            "state": "blocked",
            "checks": {"preflight_commands": "timed_out"},
            "blockers": ["preflight_timeout"],
            "evidence_grade": "unavailable",
        }
    except OSError:
        return {
            "state": "blocked",
            "checks": {"preflight_commands": "failed_to_start"},
            "blockers": ["preflight_start_failed"],
            "evidence_grade": "unavailable",
        }
    return _evaluate_active_output(
        contract.preflight_commands,
        result.stdout + "\n" + result.stderr,
        exit_code=result.returncode,
    )


def validate_preflight(
    report: Mapping[str, Any],
    contract: TaskContract | Mapping[str, Any],
    capability: Any,
    *,
    now: datetime | None = None,
    require_ready: bool = True,
) -> bool:
    parsed = TaskContract.from_mapping(contract)
    current = now or datetime.now(timezone.utc)
    try:
        expires_at = datetime.fromisoformat(str(report["expires_at"]))
    except (KeyError, ValueError):
        return False
    if expires_at.tzinfo is None:
        return False
    if expires_at <= current.astimezone(timezone.utc):
        return False
    contract_digest = parsed.digest()
    expected_material = {
        "contract_digest": contract_digest,
        "capability_id": capability.capability_id,
        "policy_digest": capability.settings_summary.get("digest"),
        "checks": report.get("checks"),
        "expires_at": report.get("expires_at"),
    }
    expected_preflight_id = hashlib.sha256(
        json.dumps(
            expected_material,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    accepted_states = {"ready"} if require_ready else {"ready", "ready_for_probe"}
    return (
        report.get("contract_digest") == contract_digest
        and report.get("capability_id") == capability.capability_id
        and report.get("policy_digest")
        == capability.settings_summary.get("digest")
        and report.get("preflight_id") == expected_preflight_id
        and report.get("state") in accepted_states
    )
