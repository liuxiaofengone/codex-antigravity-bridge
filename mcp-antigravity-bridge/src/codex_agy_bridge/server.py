"""MCP server exposing Google Antigravity (`agy`) as tools for Codex."""

# Modified by liuxiaofengone, 2026-10-10: reject CLI soft failures despite zero exit status.

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .agy_collaboration import agy_collaborations
from .agy_jobs import agy_jobs
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
def agy_ask(
    prompt: str,
    workdir: str = "",
    timeout: float = 300.0,
    dangerously_skip_permissions: bool = False,
) -> str:
    """Ask the Google Antigravity agent headlessly and return its text answer.

    Args:
        prompt: The task/instruction for the Antigravity agent.
        workdir: Optional working directory for the agy process ("" = inherit).
        timeout: Hard wall-clock timeout in seconds.
        dangerously_skip_permissions: Allow agy tools without interactive prompts.
    """
    result = run_agy(
        prompt,
        workdir=workdir or None,
        timeout=_validate_timeout(timeout),
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    return _require_success(result).text


@mcp.tool()
def agy_ask_json(
    prompt: str,
    workdir: str = "",
    timeout: float = 300.0,
    dangerously_skip_permissions: bool = False,
) -> str:
    """Ask the Google Antigravity agent and return structured JSON.

    Uses `agy -p <prompt> --output-format json`.
    """
    result = run_agy(
        prompt,
        workdir=workdir or None,
        timeout=_validate_timeout(timeout),
        output_format="json",
        dangerously_skip_permissions=dangerously_skip_permissions,
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
) -> str:
    """Start an asynchronous agy task and return its job id.

    Use this for explicit parallel worktree collaboration. The caller must
    provide an existing isolated worktree as workdir; the bridge does not
    create one. Poll the returned id with ``agy_status`` while Codex continues
    work elsewhere.
    """
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
) -> str:
    """Start an explicit multi-task collaboration session.

    ``tasks`` must contain objects with ``id``, ``prompt``, ``owned_paths``,
    and ``acceptance`` fields. Each task receives its own Git worktree and
    branch. ``display_mode='terminal'`` opens one visible Windows console per
    task. ``max_tasks`` defaults to four and is a hard upper bound for this
    session. The result is ready for manual review; this tool never merges.
    """
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
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def agy_collab_status(session_id: str) -> str:
    """Return aggregated status for an MVP collaboration session."""
    return json.dumps(agy_collaborations.status(session_id), ensure_ascii=False)
