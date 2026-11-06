"""Task-scoped AGY execution options backed by local canary evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from uuid import uuid4

from .agy_evidence import (
    BRIDGE_VERSION,
    POLICY_REQUIRED_CHECKS,
    PolicyEvidence,
    PolicyEvidenceError,
    load_verified_policy_evidence,
    save_policy_evidence,
)

if TYPE_CHECKING:
    from .agy_capabilities import CapabilitySnapshot

_MODES = {"inherit", "plan", "accept-edits", "auto"}
_OPTION_KEYS = {"mode", "approved_commands", "authorization_ref"}
_SELECTOR_SHAPE = "exact_command"


class PolicyError(RuntimeError):
    """Raised when task-scoped execution options cannot be applied safely."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecutionOptions:
    mode: str = "inherit"
    approved_commands: tuple[str, ...] = ()
    authorization_ref: str | None = None

    @property
    def is_default(self) -> bool:
        return self.mode == "inherit" and not self.approved_commands


def parse_execution_options(
    raw: Mapping[str, Any] | None,
    *,
    dangerously_skip_permissions: bool = False,
) -> ExecutionOptions:
    if raw is None:
        return ExecutionOptions()
    if not isinstance(raw, Mapping):
        raise PolicyError("execution_options must be an object", code="invalid_options")
    unknown = set(raw) - _OPTION_KEYS
    if unknown:
        raise PolicyError(
            f"unsupported execution_options keys: {', '.join(sorted(unknown))}",
            code="invalid_options",
        )
    mode = raw.get("mode", "inherit")
    if not isinstance(mode, str) or mode not in _MODES:
        raise PolicyError(
            "mode must be one of inherit, plan, accept-edits, or auto",
            code="invalid_options",
        )
    commands_raw = raw.get("approved_commands", [])
    if not isinstance(commands_raw, (list, tuple)):
        raise PolicyError(
            "approved_commands must be a list of exact command strings",
            code="invalid_options",
        )
    commands: list[str] = []
    for command in commands_raw:
        if not isinstance(command, str) or not command.strip():
            raise PolicyError(
                "approved_commands must contain non-empty strings",
                code="invalid_options",
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in command):
            raise PolicyError(
                "approved_commands entries must not contain control characters",
                code="invalid_options",
            )
        commands.append(command.strip())
    authorization_ref = raw.get("authorization_ref")
    if authorization_ref is not None:
        if not isinstance(authorization_ref, str) or not authorization_ref.strip():
            raise PolicyError(
                "authorization_ref must be a non-empty string",
                code="invalid_options",
            )
        authorization_ref = authorization_ref.strip()
    options = ExecutionOptions(
        mode=mode,
        approved_commands=tuple(dict.fromkeys(commands)),
        authorization_ref=authorization_ref,
    )
    if not options.is_default and options.authorization_ref is None:
        raise PolicyError(
            "authorization_ref is required for non-default execution_options",
            code="authorization_required",
        )
    if not options.is_default and dangerously_skip_permissions:
        raise PolicyError(
            "execution_options conflict with dangerously_skip_permissions",
            code="parameter_conflict",
        )
    return options


def _normalise_rule_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PolicyError(
            "manual_policy_required: inherited permission rules are not a string list",
            code="manual_policy_required",
        )
    return list(dict.fromkeys(item for item in value if item))


def _settings_payload(
    options: ExecutionOptions,
    inherited_permissions: Mapping[str, Any],
) -> dict[str, Any]:
    inherited = copy.deepcopy(dict(inherited_permissions))
    permissions_raw = inherited.get("permissions", {})
    if not isinstance(permissions_raw, Mapping):
        raise PolicyError(
            "manual_policy_required: inherited permissions are not an object",
            code="manual_policy_required",
        )
    permissions = {
        str(key): _normalise_rule_list(value)
        for key, value in permissions_raw.items()
    }
    allow = permissions.setdefault("allow", [])
    allow.extend(f"command({command})" for command in options.approved_commands)
    permissions["allow"] = list(dict.fromkeys(allow))
    payload: dict[str, Any] = {"permissions": permissions}
    if "toolPermission" in inherited:
        payload["toolPermission"] = inherited["toolPermission"]
    return payload


def _selector_shapes(options: ExecutionOptions) -> tuple[str, ...]:
    if options.approved_commands:
        return (_SELECTOR_SHAPE,)
    return ()


def build_session_args(
    options: ExecutionOptions,
    capabilities: "CapabilitySnapshot",
    inherited_permissions: Mapping[str, Any],
) -> list[str]:
    """Build narrow CLI flags and verify evidence on every invocation."""
    if options.is_default:
        return []
    options = parse_execution_options({
        "mode": options.mode,
        "approved_commands": list(options.approved_commands),
        "authorization_ref": options.authorization_ref,
    })
    if not capabilities.declared_flags.get("--settings"):
        raise PolicyError(
            "unsupported_capability: current AGY CLI does not declare --settings",
            code="unsupported_capability",
        )
    if options.mode != "inherit" and not capabilities.declared_flags.get("--mode"):
        raise PolicyError(
            "unsupported_capability: current AGY CLI does not declare --mode",
            code="unsupported_capability",
        )
    try:
        load_verified_policy_evidence(
            capabilities,
            options.mode,
            _selector_shapes(options),
        )
    except PolicyEvidenceError as exc:
        raise PolicyError(
            f"policy_unverified: {exc}",
            code="policy_unverified",
        ) from exc
    payload = _settings_payload(options, inherited_permissions)
    args = [
        "--settings",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ]
    if options.mode != "inherit":
        args.extend(["--mode", options.mode])
    return args


def _permission_files() -> list[Path]:
    configured_cli = os.environ.get("AGY_CLI_SETTINGS_PATH")
    configured_shared = os.environ.get("AGY_SHARED_CONFIG_PATH")
    home = Path.home()
    return [
        Path(configured_cli).expanduser()
        if configured_cli
        else home / ".gemini" / "antigravity-cli" / "settings.json",
        Path(configured_shared).expanduser()
        if configured_shared
        else home / ".gemini" / "config" / "config.json",
    ]


def load_inherited_permissions() -> dict[str, Any]:
    cli_path, shared_path = _permission_files()
    try:
        cli = json.loads(cli_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(
            "manual_policy_required: CLI settings cannot be read safely",
            code="manual_policy_required",
        ) from exc
    permissions = copy.deepcopy(cli.get("permissions", {}))
    if not isinstance(permissions, dict):
        raise PolicyError(
            "manual_policy_required: CLI permissions are malformed",
            code="manual_policy_required",
        )
    if shared_path.is_file():
        try:
            shared = json.loads(shared_path.read_text(encoding="utf-8"))
            global_grants = shared["userSettings"]["globalPermissionGrants"]
            shared_allow = _normalise_rule_list(global_grants.get("allow", []))
        except (OSError, ValueError, KeyError, TypeError, PolicyError) as exc:
            raise PolicyError(
                "manual_policy_required: shared permissions cannot be read safely",
                code="manual_policy_required",
            ) from exc
        permissions["allow"] = list(dict.fromkeys(
            _normalise_rule_list(permissions.get("allow", [])) + shared_allow
        ))
    result: dict[str, Any] = {"permissions": permissions}
    if "toolPermission" in cli:
        result["toolPermission"] = cli["toolPermission"]
    return result


def _policy_settings_snapshot(paths: list[Path] | None = None) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in paths or _permission_files():
        try:
            snapshot[str(path)] = path.read_bytes()
        except OSError:
            snapshot[str(path)] = b"<missing>"
    return snapshot


def _tool_event_confirms(output: str, marker: str, command: str) -> bool:
    command_lower = command.lower()
    marker_lower = marker.lower()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        step = event.get("step_update")
        if isinstance(step, dict) and str(step.get("step_type", "")).lower() == "tool":
            info = step.get("tool_info")
            if isinstance(info, dict):
                parameters = info.get("parameters")
                command_present = (
                    isinstance(parameters, dict)
                    and command_lower
                    in json.dumps(parameters, ensure_ascii=False).lower()
                )
                output = info.get("output")
                error = info.get("error")
                if (
                    str(step.get("state", "")).upper() == "DONE"
                    and command_present
                    and isinstance(output, str)
                    and marker_lower in output.lower()
                    and not error
                ):
                    return True
        event_type = str(event.get("type", event.get("event", ""))).lower()
        step_type = str(event.get("step_type", "")).lower()
        serialized = json.dumps(event, ensure_ascii=False).lower()
        if marker_lower not in serialized or command_lower not in serialized:
            continue
        if any(
            token in f"{event_type} {step_type}"
            for token in ("tool", "function", "command", "run")
        ):
            return True
        if any(
            key in event
            for key in ("tool_call_id", "toolCallId", "functionResponse")
        ):
            return True
    return False


def _event_was_denied(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in (
            "denied_actions",
            "user denied permission",
            "permission check failed",
            "permission denied",
            "matches user-configured deny rule",
        )
    )


def _event_summary(output: str, marker: str, command: str) -> dict[str, Any]:
    event_types: list[str] = []
    top_level_keys: set[str] = set()
    type_values: list[str] = []
    tool_shapes: list[dict[str, Any]] = []
    marker_present = marker.lower() in output.lower()
    command_present = command.lower() in output.lower()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        top_level_keys.update(str(key) for key in event)
        event_type = str(event.get("type", event.get("event", "unknown")))
        step_type = event.get("step_type")
        label = f"{event_type}:{step_type}" if step_type else event_type
        if label not in event_types:
            event_types.append(label)
        stack: list[Any] = [event]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                type_items = [
                    (str(key), str(item))
                    for key, item in value.items()
                    if "type" in str(key).lower() and isinstance(item, str)
                ]
                if any(
                    item.lower() in {"tool", "tool_error", "tool_call"}
                    for _, item in type_items
                ):
                    safe_scalars = {
                        str(key): item
                        for key, item in value.items()
                        if str(key).lower()
                        not in {
                            "command",
                            "content",
                            "details",
                            "error",
                            "input",
                            "message",
                            "output",
                            "prompt",
                            "text",
                        }
                        and isinstance(item, (str, int, bool))
                    }
                    shape = {
                        "type_fields": type_items,
                        "keys": sorted(str(key) for key in value),
                        "safe_scalars": safe_scalars,
                    }
                    rendered_shape = json.dumps(shape, ensure_ascii=False, sort_keys=True)
                    if all(
                        json.dumps(existing, ensure_ascii=False, sort_keys=True)
                        != rendered_shape
                        for existing in tool_shapes
                    ):
                        tool_shapes.append(shape)
                for key, item in value.items():
                    key_text = str(key).lower()
                    if "type" in key_text and isinstance(item, (str, int, bool)):
                        rendered = str(item)
                        if rendered not in type_values:
                            type_values.append(rendered)
                    if isinstance(item, (dict, list)):
                        stack.append(item)
            elif isinstance(value, list):
                stack.extend(value)
    return {
        "event_types": event_types,
        "top_level_keys": sorted(top_level_keys),
        "type_values": type_values,
        "tool_shapes": tool_shapes,
        "marker_present": marker_present,
        "command_present": command_present,
        "tool_execution_confirmed": _tool_event_confirms(output, marker, command),
        "denial_observed": _event_was_denied(output),
    }


def _run_canary_call(
    binary: str,
    prompt: str,
    workdir: Path,
    timeout: float,
    args: list[str],
) -> str:
    command = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        *args,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})
    return result.stdout + "\n" + result.stderr


def probe_agy_policy(authorization_ref: str, timeout: float = 60.0) -> dict[str, Any]:
    """Run three bounded local fixture calls and persist only passing evidence."""
    from .agy_capabilities import (
        CapabilityDiscoveryError,
        _settings_paths,
        inspect_agy_capabilities,
    )
    from .agy_runner import find_agy

    if os.environ.get("AGY_POLICY_PROBE_OPT_IN") != "1":
        return {
            "state": "policy_unverified",
            "error": "AGY_POLICY_PROBE_OPT_IN=1 is required for an active canary",
            "checks": {},
        }
    try:
        options = parse_execution_options({
            "mode": "accept-edits",
            "approved_commands": [],
            "authorization_ref": authorization_ref,
        })
    except PolicyError as exc:
        return {"state": "policy_unverified", "error": str(exc), "checks": {}}
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        return {
            "state": "policy_unverified",
            "error": "timeout must be positive",
            "checks": {},
        }
    binary = find_agy()
    if not binary:
        return {
            "state": "policy_unverified",
            "error": "agy binary not found",
            "checks": {},
        }
    try:
        capabilities = inspect_agy_capabilities(refresh=True)
    except CapabilityDiscoveryError as exc:
        return {"state": "policy_unverified", "error": str(exc), "checks": {}}
    if not capabilities.declared_flags.get("--settings"):
        return {
            "state": "policy_unverified",
            "error": "current CLI does not declare --settings",
            "checks": {},
        }
    try:
        inherited = load_inherited_permissions()
    except PolicyError as exc:
        return {"state": "policy_unverified", "error": str(exc), "checks": {}}

    marker = f"AGY_POLICY_CANARY_{uuid4().hex[:12].upper()}"
    exact_command = f"cmd /c echo {marker}"
    if sys.platform != "win32":
        exact_command = f"sh -c 'printf {marker}'"
    scoped_options = ExecutionOptions(
        mode="accept-edits",
        approved_commands=(exact_command,),
        authorization_ref=options.authorization_ref,
    )
    scoped_payload = _settings_payload(scoped_options, inherited)
    scoped_args = [
        "--settings",
        json.dumps(scoped_payload, ensure_ascii=False, separators=(",", ":")),
        "--mode",
        "accept-edits",
    ]
    prompt = (
        f"Run exactly `{exact_command}` once and return its literal output. "
        "Do not run any other tool or command."
    )
    settings_paths = _settings_paths()
    before = _policy_settings_snapshot(settings_paths)
    fixture = Path(tempfile.mkdtemp(prefix="codex-agy-policy-"))
    try:
        subprocess.run(
            ["git", "init"],
            cwd=fixture,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        (fixture / "README.md").write_text("policy canary\n", encoding="utf-8")
        for git_args in (
            ("config", "user.email", "policy-canary@example.invalid"),
            ("config", "user.name", "Policy Canary"),
            ("add", "README.md"),
            ("commit", "-m", "policy canary"),
        ):
            subprocess.run(
                ["git", *git_args],
                cwd=fixture,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
        try:
            session_a = _run_canary_call(
                binary, prompt, fixture, float(timeout), scoped_args
            )
            session_b = _run_canary_call(
                binary, prompt, fixture, float(timeout), []
            )
            denied_payload = copy.deepcopy(scoped_payload)
            denied_payload["permissions"]["deny"] = [
                f"command({exact_command})"
            ]
            session_c_args = [
                "--settings",
                json.dumps(denied_payload, ensure_ascii=False, separators=(",", ":")),
                "--mode",
                "accept-edits",
            ]
            session_c = _run_canary_call(
                binary, prompt, fixture, float(timeout), session_c_args
            )
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        after = _policy_settings_snapshot(settings_paths)
        checks = {
            "approved_command_executed": _tool_event_confirms(
                session_a, marker, exact_command
            ),
            "unapproved_command_blocked": not _tool_event_confirms(
                session_b, marker, exact_command
            ),
            "deny_ask_preserved": not _tool_event_confirms(
                session_c, marker, exact_command
            ) and _event_was_denied(session_c),
            "global_settings_unchanged": before == after,
            "session_isolation": not _tool_event_confirms(
                session_b, marker, exact_command
            ),
        }
        diagnostics = {
            "session_a": _event_summary(session_a, marker, exact_command),
            "session_b": _event_summary(session_b, marker, exact_command),
            "session_c": _event_summary(session_c, marker, exact_command),
        }
        if not all(checks.get(name) for name in POLICY_REQUIRED_CHECKS):
            return {
                "state": "policy_unverified",
                "checks": checks,
                "diagnostics": diagnostics,
                "error": "local canary did not produce complete evidence",
            }
        now = datetime.now(timezone.utc)
        evidence_id = hashlib.sha256(
            f"{capabilities.capability_id}:{marker}".encode()
        ).hexdigest()[:20]
        evidence = PolicyEvidence(
            evidence_id=evidence_id,
            cli_fingerprint=capabilities.capability_id,
            base_policy_digest=str(
                capabilities.settings_summary["digest"]
            ),
            platform=capabilities.python_platform,
            builder_version=BRIDGE_VERSION,
            tested_modes=("inherit", "accept-edits"),
            selector_shapes=(_SELECTOR_SHAPE,),
            checks=checks,
            verified_at=now.isoformat(),
            expires_at=(now + timedelta(hours=24)).isoformat(),
        )
        save_policy_evidence(evidence)
        return {
            "state": "verified",
            "evidence_id": evidence.evidence_id,
            "checks": checks,
            "diagnostics": diagnostics,
            "expires_at": evidence.expires_at,
        }
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
