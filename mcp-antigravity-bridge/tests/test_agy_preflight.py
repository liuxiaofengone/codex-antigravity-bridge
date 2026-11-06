"""Task contract preflight tests with no implicit model calls."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_agy_bridge import agy_preflight, server
from codex_agy_bridge.agy_capabilities import CapabilitySnapshot


def _capabilities(capability_id: str = "cap-1") -> CapabilitySnapshot:
    return CapabilitySnapshot(
        capability_id=capability_id,
        binary_path="C:\\agy.exe",
        cli_version="1.5.1",
        python_platform="Windows-ARM64-Python 3.14.10",
        declared_flags={"--settings": True, "--mode": True},
        mode_availability={
            "accept-edits": "unknown",
            "auto": "unknown",
            "plan": "unknown",
        },
        settings_summary={"digest": "policy-digest"},
    )


def _contract(tmp_path: Path, **overrides):
    values = {
        "task_id": "task-1",
        "capability_id": "cap-1",
        "execution_target": "local",
        "local_dir": str(tmp_path),
        "mode": "accept-edits",
        "approved_commands": ["git status --short"],
        "authorization_ref": "user-approved-1",
        "owned_paths": ["src"],
        "forbidden_paths": ["production"],
        "preflight_commands": ["git status --short"],
        "requires_hard_tool_interception": False,
    }
    values.update(overrides)
    return values


@pytest.fixture
def static_dependencies(monkeypatch):
    monkeypatch.setattr(
        agy_preflight,
        "inspect_agy_capabilities",
        lambda refresh=False: _capabilities(),
    )
    monkeypatch.setattr(
        agy_preflight,
        "load_verified_policy_evidence",
        lambda *args, **kwargs: SimpleNamespace(evidence_id="evidence-1"),
    )
    monkeypatch.setattr(
        agy_preflight,
        "load_inherited_permissions",
        lambda: {"permissions": {"allow": []}},
    )
    monkeypatch.setattr(
        agy_preflight,
        "_run_active_probe",
        lambda *args, **kwargs: pytest.fail("static preflight must not run AGY"),
    )


def test_static_preflight_is_side_effect_free(
    tmp_path: Path, monkeypatch, static_dependencies
) -> None:
    report = agy_preflight.preflight(_contract(tmp_path), active_probe=False)

    assert report["state"] == "ready_for_probe"
    assert report["evidence_grade"] == "unavailable"
    assert report["enforcement"] == "policy_and_prompt"
    assert report["checks"]["capability"] == "passed"
    assert report["checks"]["policy_evidence"] == "passed"
    assert report["blockers"] == []
    assert report["expires_at"]


def test_authorization_and_plan_conflicts_are_blocked(
    tmp_path: Path, static_dependencies
) -> None:
    missing_auth = agy_preflight.preflight(
        _contract(tmp_path, authorization_ref=None),
        active_probe=False,
    )
    plan_execution = agy_preflight.preflight(
        _contract(tmp_path, mode="plan"),
        active_probe=False,
    )

    assert missing_auth["state"] == "blocked"
    assert "authorization_ref_required" in missing_auth["blockers"]
    assert plan_execution["state"] == "blocked"
    assert "plan_mode_cannot_execute_preflight" in plan_execution["blockers"]


def test_capability_change_and_missing_directory_are_blocked(
    tmp_path: Path, static_dependencies
) -> None:
    changed = agy_preflight.preflight(
        _contract(tmp_path, capability_id="old-capability"),
        active_probe=False,
    )
    missing = agy_preflight.preflight(
        _contract(tmp_path, local_dir=str(tmp_path / "missing")),
        active_probe=False,
    )

    assert changed["state"] == "blocked"
    assert "capability_changed" in changed["blockers"]
    assert missing["state"] == "blocked"
    assert "local_dir_missing" in missing["blockers"]


def test_missing_policy_evidence_is_unknown(
    tmp_path: Path, monkeypatch, static_dependencies
) -> None:
    def missing(*args, **kwargs):
        raise agy_preflight.PolicyEvidenceError("not available")

    monkeypatch.setattr(agy_preflight, "load_verified_policy_evidence", missing)

    report = agy_preflight.preflight(_contract(tmp_path), active_probe=False)

    assert report["state"] == "unknown"
    assert "policy_unverified" in report["blockers"]


def test_remote_context_is_never_treated_as_a_windows_cwd(
    tmp_path: Path, static_dependencies
) -> None:
    incomplete = agy_preflight.preflight(
        _contract(tmp_path, execution_target="remote_ssh"),
        active_probe=False,
    )
    complete = agy_preflight.preflight(
        _contract(
            tmp_path,
            execution_target="remote_ssh",
            remote_context={
                "workspace_id": "workspace-1",
                "repository": "owner/repo",
                "ssh_alias": "cnb-dev",
                "worktree": "/workspace/repo",
                "branch": "codex/task",
                "baseline_sha": "a" * 40,
                "evidence_refs": ["artifact-1"],
            },
        ),
        active_probe=False,
    )

    assert incomplete["state"] == "blocked"
    assert "remote_context_identity_incomplete" in incomplete["blockers"]
    assert Path("/workspace/repo").exists() is False
    assert complete["state"] == "unknown", complete["blockers"]
    assert "remote_identity_not_read" in complete["blockers"]
    no_refs = agy_preflight.preflight(
        _contract(
            tmp_path,
            execution_target="remote_ssh",
            remote_context={
                "workspace_id": "workspace-1",
                "repository": "owner/repo",
                "ssh_alias": "cnb-dev",
                "worktree": "/workspace/repo",
                "branch": "codex/task",
                "baseline_sha": "a" * 40,
            },
        ),
        active_probe=False,
    )
    assert no_refs["state"] == "blocked"
    assert "remote_context_identity_incomplete" in no_refs["blockers"]


def test_hard_interception_requirement_is_unsupported(
    tmp_path: Path, static_dependencies
) -> None:
    report = agy_preflight.preflight(
        _contract(tmp_path, requires_hard_tool_interception=True),
        active_probe=False,
    )

    assert report["state"] == "blocked"
    assert report["enforcement"] == "unsupported_enforcement"
    assert "hard_tool_interception_unsupported" in report["blockers"]


def test_preflight_binding_checks_contract_capability_and_expiry(
    tmp_path: Path, static_dependencies
) -> None:
    contract = agy_preflight.TaskContract.from_mapping(_contract(tmp_path))
    report = agy_preflight.preflight(contract, active_probe=False)

    assert agy_preflight.validate_preflight(
        report,
        contract,
        _capabilities(),
        require_ready=False,
    ) is True
    assert agy_preflight.validate_preflight(
        report, contract, _capabilities()
    ) is False
    assert agy_preflight.validate_preflight(
        report,
        contract,
        _capabilities("changed"),
        require_ready=False,
    ) is False
    assert agy_preflight.validate_preflight(
        report,
        contract,
        _capabilities(),
        now=datetime.now(timezone.utc) + timedelta(minutes=6),
        require_ready=False,
    ) is False
    tampered = dict(report)
    tampered["preflight_id"] = "tampered"
    assert agy_preflight.validate_preflight(
        tampered,
        contract,
        _capabilities(),
        require_ready=False,
    ) is False


def test_active_probe_requires_explicit_opt_in(
    tmp_path: Path, monkeypatch, static_dependencies
) -> None:
    monkeypatch.delenv("AGY_PREFLIGHT_PROBE_OPT_IN", raising=False)

    report = agy_preflight.preflight(_contract(tmp_path), active_probe=True)

    assert report["state"] == "blocked"
    assert "active_probe_opt_in_required" in report["blockers"]


def test_active_probe_success_requires_cli_tool_evidence(
    tmp_path: Path, monkeypatch, static_dependencies
) -> None:
    monkeypatch.setenv("AGY_PREFLIGHT_PROBE_OPT_IN", "1")
    calls = []
    monkeypatch.setattr(
        agy_preflight,
        "_run_active_probe",
        lambda *args, **kwargs: calls.append(True) or {
            "state": "ready",
            "checks": {"preflight_commands": "passed"},
            "blockers": [],
            "evidence_grade": "cli_tool_result",
        },
    )

    report = agy_preflight.preflight(_contract(tmp_path), active_probe=True)

    assert report["state"] == "ready"
    assert report["evidence_grade"] == "cli_tool_result"
    assert calls == [True]


def _probe_event(
    command: str,
    *,
    state: str,
    output: str | None = None,
    error: str = "",
):
    info = {
        "name": "run_command",
        "parameters": {"CommandLine": command},
    }
    if output is not None:
        info["output"] = output
    if error:
        info["error"] = error
    return json.dumps({
        "event": "step_update",
        "step_update": {
            "state": state,
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": info,
        },
    })


def test_active_output_success_requires_done_tool_result() -> None:
    command = "git status --short"
    output = "\n".join([
        _probe_event(command, state="ACTIVE"),
        _probe_event(command, state="DONE", output=""),
    ])

    result = agy_preflight._evaluate_active_output((command,), output)

    assert result["state"] == "ready"
    assert result["evidence_grade"] == "cli_tool_result"


def test_active_output_model_self_report_is_unknown() -> None:
    command = "git status --short"
    output = json.dumps({
        "event": "result",
        "result": {"response": "git status --short completed", "denied_actions": []},
    })

    result = agy_preflight._evaluate_active_output((command,), output)

    assert result["state"] == "unknown"
    assert result["evidence_grade"] == "agent_report_only"
    assert "model_report_only" in result["blockers"]


def test_active_output_permission_error_is_blocked() -> None:
    command = "cnb status"
    output = _probe_event(
        command,
        state="ERROR",
        error="Permission denied: auto-denied in headless mode",
    )

    result = agy_preflight._evaluate_active_output((command,), output)

    assert result["state"] == "blocked"
    assert result["evidence_grade"] == "cli_tool_result"
    assert "permission_blocked" in result["blockers"]


def test_active_output_non_permission_error_is_not_mislabeled() -> None:
    command = "python missing-script.py"
    output = _probe_event(
        command,
        state="ERROR",
        error="python: unable to open file",
    )

    result = agy_preflight._evaluate_active_output((command,), output)

    assert result["state"] == "blocked"
    assert "tool_error" in result["blockers"]
    assert "permission_blocked" not in result["blockers"]


def test_result_event_denied_actions_blocks_without_tool_prose() -> None:
    command = "cnb status"
    output = json.dumps({
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": "",
            "denied_actions": [{"action": "RunCommand"}],
        },
    })

    result = agy_preflight._evaluate_active_output((command,), output)

    assert result["state"] == "blocked"
    assert result["evidence_grade"] == "cli_tool_result"
    assert "permission_blocked" in result["blockers"]


@pytest.mark.parametrize(
    "observation,expected_state,blocker",
    [
        (
            {
                "state": "blocked",
                "checks": {"preflight_commands": "denied"},
                "blockers": ["permission_blocked"],
                "evidence_grade": "cli_tool_result",
            },
            "blocked",
            "permission_blocked",
        ),
        (
            {
                "state": "unknown",
                "checks": {"preflight_commands": "unavailable"},
                "blockers": ["model_report_only"],
                "evidence_grade": "agent_report_only",
            },
            "unknown",
            "model_report_only",
        ),
    ],
)
def test_denial_and_self_report_cannot_become_ready(
    tmp_path: Path,
    monkeypatch,
    static_dependencies,
    observation,
    expected_state,
    blocker,
) -> None:
    monkeypatch.setenv("AGY_PREFLIGHT_PROBE_OPT_IN", "1")
    monkeypatch.setattr(
        agy_preflight,
        "_run_active_probe",
        lambda *args, **kwargs: observation,
    )

    report = agy_preflight.preflight(_contract(tmp_path), active_probe=True)

    assert report["state"] == expected_state
    assert blocker in report["blockers"]


def test_server_preflight_wrapper_forwards_contract(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}
    monkeypatch.setattr(
        server,
        "preflight",
        lambda contract, active_probe=False: captured.update({
            "contract": contract,
            "active_probe": active_probe,
        }) or {"state": "unknown"},
    )

    payload = json.loads(server.agy_preflight(_contract(tmp_path), active_probe=False))

    assert payload["state"] == "unknown"
    assert captured["active_probe"] is False
