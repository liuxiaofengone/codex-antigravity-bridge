"""MCP server exposing Google Antigravity (`agy`) as tools for Codex."""

# Modified by liuxiaofengone, 2026-10-10: reject CLI soft failures despite zero exit status.

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .agy_capabilities import inspect_agy_capabilities
from .agy_collaboration import agy_collaborations
from .agy_evidence import PolicyEvidenceError, load_policy_evidence
from .agy_jobs import agy_jobs
from .agy_policy import parse_execution_options, probe_agy_policy
from .agy_preflight import preflight
from .agy_runner import AgyResult, agy_result_error_kind, describe_agy_failure, run_agy

mcp = FastMCP(
    "codex-agy-bridge",
    instructions=(
        "Bridge from Codex to the Google Antigravity agent. "
        "Use agy_ask for a one-shot headless call (`agy -p`); "
        "use agy_ask_json when you want structured JSON output; "
        "use agy_start and agy_status for explicit asynchronous worktree collaboration "
        "with a caller-created isolated workdir. "
        "Use agy_collab_start and agy_collab_status for the MVP collaboration mode: "
        "the bridge creates separate Git worktrees and starts bounded tasks, but "
        "Codex reviews and merges branches manually. "
        "Only pass dangerously_skip_permissions=true after the user explicitly "
        "authorizes that exact trusted worktree and task."
    ),
)


def _require_success(result: AgyResult) -> AgyResult:
    if agy_result_error_kind(result) is not None:
        raise RuntimeError(describe_agy_failure(result))
    return result


def _validate_timeout(timeout: float) -> float:
    """Reject invalid MCP timeouts before starting a subprocess or job."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    return float(timeout)


@mcp.tool()
def agy_doctor(refresh: bool = False) -> str:
    """Inspect the local AGY CLI without starting a model prompt.

    The snapshot reports live `--version` and `--help` declarations plus a
    redacted fingerprint of local settings. Declared features and execution
    modes remain unknown until a separately authorized local canary verifies
    their runtime behavior.
    """
    payload = inspect_agy_capabilities(refresh=refresh).as_dict()
    try:
        payload["policy_evidence"] = load_policy_evidence().as_dict()
    except PolicyEvidenceError:
        payload["policy_evidence"] = {"state": "policy_unverified"}
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def agy_policy_probe(authorization_ref: str, timeout: float = 60.0) -> str:
    """Run an explicitly authorized local canary for session-settings safety.

    This submits bounded AGY calls against a temporary local Git repository.
    It never contacts CNB or SSH and does not modify global permission files.
    """
    return json.dumps(
        probe_agy_policy(
            authorization_ref,
            timeout=_validate_timeout(timeout),
        ),
        ensure_ascii=False,
    )


@mcp.tool()
def agy_preflight(
    contract: dict[str, Any],
    active_probe: bool = False,
) -> str:
    """Validate a bounded AGY task contract without implicit model calls.

    Static preflight only inspects local CLI/settings metadata. Set
    ``active_probe=true`` and the explicit environment opt-in to submit one
    bounded preflight prompt. A model's self-report never becomes ready evidence.
    """
    return json.dumps(
        preflight(contract, active_probe=active_probe),
        ensure_ascii=False,
    )


@mcp.tool()
def agy_ask(
    prompt: str,
    workdir: str = "",
    timeout: float = 300.0,
    dangerously_skip_permissions: bool = False,
    execution_options: dict[str, Any] | None = None,
) -> str:
    """Ask the Google Antigravity agent headlessly and return its text answer.

    Args:
        prompt: The task/instruction for the Antigravity agent.
        workdir: Optional working directory for the agy process ("" = inherit).
        timeout: Hard wall-clock timeout in seconds.
        dangerously_skip_permissions: Allow agy tools without interactive prompts.
    """
    options = parse_execution_options(
        execution_options,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    result = run_agy(
        prompt,
        workdir=workdir or None,
        timeout=_validate_timeout(timeout),
        dangerously_skip_permissions=dangerously_skip_permissions,
        execution_options=options,
    )
    return _require_success(result).text


@mcp.tool()
def agy_ask_json(
    prompt: str,
    workdir: str = "",
    timeout: float = 300.0,
    dangerously_skip_permissions: bool = False,
    execution_options: dict[str, Any] | None = None,
) -> str:
    """Ask the Google Antigravity agent and return structured JSON.

    Uses `agy -p <prompt> --output-format json`.
    """
    options = parse_execution_options(
        execution_options,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    result = run_agy(
        prompt,
        workdir=workdir or None,
        timeout=_validate_timeout(timeout),
        output_format="json",
        dangerously_skip_permissions=dangerously_skip_permissions,
        execution_options=options,
    )
    _require_success(result)
    try:
        json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise ValueError("agy_ask_json did not return valid JSON") from exc
    return result.text


@mcp.tool()
def agy_start(
    prompt: str,
    workdir: str = "",
    timeout: float = 300.0,
    dangerously_skip_permissions: bool = False,
    execution_options: dict[str, Any] | None = None,
) -> str:
    """Start an asynchronous agy task and return its job id.

    Use this for explicit parallel worktree collaboration. The caller must
    provide an existing isolated worktree as workdir; the bridge does not
    create one. Poll the returned id with ``agy_status`` while Codex continues
    work elsewhere.
    """
    options = parse_execution_options(
        execution_options,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    if not workdir.strip():
        raise ValueError(
            "agy_start requires an explicit workdir for a caller-created isolated worktree"
        )
    if not Path(workdir).expanduser().is_dir():
        raise ValueError(f"agy_start workdir is not an existing directory: {workdir}")

    return agy_jobs.start(
        prompt,
        workdir=workdir or None,
        timeout=_validate_timeout(timeout),
        dangerously_skip_permissions=dangerously_skip_permissions,
        execution_options=options,
    )


@mcp.tool()
def agy_status(job_id: str) -> str:
    """Return JSON status for an asynchronous agy task."""
    return json.dumps(agy_jobs.status(job_id), ensure_ascii=False)


@mcp.tool()
def agy_collab_start(
    project_dir: str,
    tasks: list[dict[str, Any]],
    shared_contract: str = "",
    base_ref: str = "HEAD",
    worktree_root: str = "",
    timeout: float = 900.0,
    dangerously_skip_permissions: bool = False,
    display_mode: str = "headless",
    max_tasks: int = 4,
    dry_run: bool = False,
    execution_options: dict[str, Any] | None = None,
) -> str:
    """Start an explicit multi-task collaboration session.

    ``tasks`` must contain objects with ``id``, ``prompt``, ``owned_paths``,
    and ``acceptance`` fields. Each task receives its own Git worktree and
    branch. ``display_mode='terminal'`` opens one visible Windows console per
    task. ``max_tasks`` defaults to four and is a hard upper bound for this
    session. The result is ready for manual review; this tool never merges.
    """
    options = parse_execution_options(
        execution_options,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    result = agy_collaborations.start(
        project_dir=project_dir,
        tasks=tasks,
        shared_contract=shared_contract,
        base_ref=base_ref,
        worktree_root=worktree_root,
        timeout=_validate_timeout(timeout),
        dangerously_skip_permissions=dangerously_skip_permissions,
        display_mode=display_mode,
        max_tasks=max_tasks,
        dry_run=dry_run,
        execution_options=options,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def agy_collab_status(session_id: str) -> str:
    """Return aggregated status for an MVP collaboration session."""
    return json.dumps(agy_collaborations.status(session_id), ensure_ascii=False)
