"""Remote CNB/SSH task contracts without pretending to control remote agents."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .agy_capabilities import inspect_agy_capabilities
from .agy_jobs import AgyJobRegistry, agy_jobs
from .agy_policy import ExecutionOptions, PolicyError, parse_execution_options
from .agy_preflight import (
    TaskContract,
    _remote_identity_problem,
    load_preflight_entry,
    validate_preflight,
)

_HEX_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_UNSAFE_KEY = re.compile(
    r"(?i)(?:private[_-]?key|password|passwd|secret|token|credential|authorization)"
)
_REMOTE_FIELDS = (
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
)


class RemoteStartError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _contains_unsafe_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _UNSAFE_KEY.search(str(key)) or _contains_unsafe_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_unsafe_key(item) for item in value)
    return False


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise RemoteStartError(
            f"{name} must be a list of non-empty strings",
            code="invalid_remote_context",
        )
    return tuple(item.strip() for item in value)


def _remote_path_list(value: Any, name: str) -> tuple[str, ...]:
    items = _string_list(value, name)
    for item in items:
        normalized = item.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise RemoteStartError(
                f"{name} must contain POSIX-relative paths",
                code="invalid_remote_context",
            )
        if normalized == "." or ".." in normalized.split("/"):
            raise RemoteStartError(
                f"{name} paths must not escape the remote worktree",
                code="invalid_remote_context",
            )
    return items


def _validate_remote_context(context: Mapping[str, Any]) -> dict[str, Any]:
    if _contains_unsafe_key(context):
        raise RemoteStartError(
            "remote_context must not contain SSH keys or credentials",
            code="credentials_forbidden",
        )
    missing = [key for key in _REMOTE_FIELDS if key not in context]
    if missing:
        raise RemoteStartError(
            f"remote_context is missing: {', '.join(missing)}",
            code="invalid_remote_context",
        )
    for key in (
        "workspace_id",
        "repository",
        "ssh_alias",
        "worktree",
        "branch",
        "baseline_sha",
        "target_kind",
    ):
        if not isinstance(context.get(key), str) or not str(context[key]).strip():
            raise RemoteStartError(
                f"remote_context.{key} must be non-empty",
                code="invalid_remote_context",
            )
    worktree = PurePosixPath(str(context["worktree"]))
    if not worktree.is_absolute() or ".." in worktree.parts:
        raise RemoteStartError(
            "remote_context.worktree must be a normalized POSIX absolute path",
            code="invalid_remote_context",
        )
    if not _HEX_SHA.fullmatch(str(context["baseline_sha"])):
        raise RemoteStartError(
            "remote_context.baseline_sha must be a 40-character SHA",
            code="invalid_remote_context",
        )
    owned = _remote_path_list(context["owned_paths"], "owned_paths")
    forbidden = _remote_path_list(context["forbidden_paths"], "forbidden_paths")
    evidence = _string_list(context["evidence_refs"], "evidence_refs")
    if not evidence:
        raise RemoteStartError(
            "remote_context.evidence_refs must not be empty",
            code="invalid_remote_context",
        )
    return {
        **dict(context),
        "owned_paths": list(owned),
        "forbidden_paths": list(forbidden),
        "evidence_refs": list(evidence),
    }


def _contract_context_matches(
    contract: TaskContract,
    context: Mapping[str, Any],
) -> bool:
    stored = contract.remote_context
    if not isinstance(stored, Mapping):
        return False
    return all(stored.get(key) == context.get(key) for key in _REMOTE_FIELDS)


def _options_match(
    options: ExecutionOptions,
    contract: TaskContract,
) -> bool:
    return (
        options.mode == contract.mode
        and options.approved_commands == contract.approved_commands
        and options.authorization_ref == contract.authorization_ref
    )


def start_remote(
    prompt: str,
    coordinator_dir: str,
    remote_context: Mapping[str, Any],
    preflight_id: str,
    execution_options: Mapping[str, Any] | None = None,
    timeout: float = 900.0,
    *,
    jobs: AgyJobRegistry = agy_jobs,
) -> dict[str, Any]:
    """Start one local managed AGY job bound to a verified remote context."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    coordinator = Path(coordinator_dir).expanduser()
    if not coordinator.is_dir():
        raise ValueError("coordinator_dir must be an existing local directory")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    context = _validate_remote_context(remote_context)
    if context.get("requires_remote_termination_confirmation") or context.get(
        "automatic_remote_cleanup"
    ):
        raise RemoteStartError(
            "automatic remote stop or cleanup is not supported",
            code="unsupported_remote_lifecycle",
        )
    if context["target_kind"] != "development":
        raise RemoteStartError(
            "only development targets are supported",
            code="non_development_target",
        )
    entry = load_preflight_entry(preflight_id)
    if entry is None:
        raise RemoteStartError(
            "preflight report is unknown or expired",
            code="preflight_expired_or_unknown",
        )
    report, stored_contract = entry
    if stored_contract.execution_target != "remote_ssh":
        raise RemoteStartError(
            "preflight contract is not a remote_ssh task",
            code="preflight_target_mismatch",
        )
    if not _contract_context_matches(stored_contract, context):
        raise RemoteStartError(
            "remote context does not match the preflight contract",
            code="remote_context_changed",
        )
    try:
        options = parse_execution_options(execution_options)
    except PolicyError as exc:
        raise RemoteStartError(str(exc), code=exc.code) from exc
    if not _options_match(options, stored_contract):
        raise RemoteStartError(
            "execution_options do not match the preflight policy",
            code="policy_binding_changed",
        )
    capability = inspect_agy_capabilities(refresh=True)
    if not validate_preflight(
        report,
        stored_contract,
        capability,
        require_ready=False,
    ):
        raise RemoteStartError(
            "preflight binding changed or expired",
            code="preflight_stale_or_changed",
        )
    if report.get("checks", {}).get("remote_identity") != "caller_verified":
        raise RemoteStartError(
            "remote identity is not independently verified",
            code="remote_identity_unverified",
        )
    identity_problem = _remote_identity_problem(
        context,
        now=datetime.now(timezone.utc),
    )
    if identity_problem:
        raise RemoteStartError(
            f"remote identity cannot be used: {identity_problem}",
            code=identity_problem,
        )

    context_digest = hashlib.sha256(
        json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    metadata = {
        "remote_termination": "unknown",
        "remote_context_digest": context_digest,
        "remote_evidence_refs": list(context["evidence_refs"]),
        "remote_identity_status": "caller_verified",
        "business_acceptance": "not_started",
    }
    job_id = jobs.start(
        prompt,
        workdir=str(coordinator.resolve()),
        timeout=float(timeout),
        output_format="stream-json",
        execution_options=options,
        process_control="managed",
        metadata=metadata,
    )
    return {
        "job_id": job_id,
        "state": "queued",
        "local_job_status": "queued",
        "process_control": "managed",
        "cancellation_support": "local_process_tree",
        "remote_termination": "unknown",
        "business_acceptance": "not_started",
        "remote_context_digest": context_digest,
        "coordination_evidence": {
            "workspace_id": context["workspace_id"],
            "repository": context["repository"],
            "worktree": context["worktree"],
            "branch": context["branch"],
            "baseline_sha": context["baseline_sha"],
            "evidence_refs": list(context["evidence_refs"]),
            "identity_observed_at": context["identity"]["observed_at"],
        },
    }
