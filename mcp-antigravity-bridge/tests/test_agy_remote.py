"""CNB/SSH contract and remote-lifecycle boundary tests."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from codex_agy_bridge import agy_preflight, agy_remote, server
from codex_agy_bridge.agy_capabilities import CapabilitySnapshot
from codex_agy_bridge.agy_jobs import AgyJobRegistry
from codex_agy_bridge.agy_process import ManagedProcessOutcome
from codex_agy_bridge.agy_remote import RemoteStartError
from codex_agy_bridge.agy_runner import ManagedAgyInvocation


def _capabilities(capability_id: str = "cap-remote") -> CapabilitySnapshot:
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
        settings_summary={"digest": "remote-policy"},
    )


def _remote_context(tmp_path, **overrides):
    now = datetime.now(timezone.utc).isoformat()
    values = {
        "workspace_id": "cnb-workspace-1",
        "repository": "nexthorizon/example",
        "ssh_alias": "cnb-dev",
        "worktree": "/workspace/.worktrees/task-1",
        "branch": "codex/task-1",
        "baseline_sha": "a" * 40,
        "target_kind": "development",
        "owned_paths": ["apps/server"],
        "forbidden_paths": ["production", ".git"],
        "evidence_refs": ["artifact://cnb-workspace-1/identity"],
        "identity": {
            "workspace_id": "cnb-workspace-1",
            "repository": "nexthorizon/example",
            "worktree": "/workspace/.worktrees/task-1",
            "branch": "codex/task-1",
            "baseline_sha": "a" * 40,
            "observed_at": now,
            "source": "caller_verified",
        },
        "coordinator_dir": str(tmp_path),
    }
    values.update(overrides)
    return values


def _contract(tmp_path, remote_context):
    return {
        "task_id": "remote-task-1",
        "capability_id": "cap-remote",
        "execution_target": "remote_ssh",
        "local_dir": str(tmp_path),
        "mode": "accept-edits",
        "approved_commands": ["git status --short"],
        "authorization_ref": "user-approved-remote-1",
        "owned_paths": remote_context["owned_paths"],
        "forbidden_paths": remote_context["forbidden_paths"],
        "preflight_commands": ["git status --short"],
        "remote_context": remote_context,
    }


@pytest.fixture
def remote_dependencies(monkeypatch):
    agy_preflight.clear_preflight_reports()
    capabilities = _capabilities()
    monkeypatch.setattr(
        agy_preflight,
        "inspect_agy_capabilities",
        lambda refresh=False: capabilities,
    )
    monkeypatch.setattr(
        agy_preflight,
        "load_verified_policy_evidence",
        lambda *args, **kwargs: SimpleNamespace(evidence_id="policy-evidence"),
    )
    monkeypatch.setattr(
        agy_preflight,
        "load_inherited_permissions",
        lambda: {"permissions": {"allow": []}},
    )
    monkeypatch.setattr(
        agy_remote,
        "inspect_agy_capabilities",
        lambda refresh=False: capabilities,
    )
    return capabilities


class FakeJobs:
    def __init__(self):
        self.started = []

    def start(self, prompt, **kwargs):
        self.started.append({"prompt": prompt, **kwargs})
        return "remote-job-1"


def _preflight_report(tmp_path, context):
    return agy_preflight.preflight(_contract(tmp_path, context), active_probe=False)


def test_remote_start_uses_coordinator_only_and_managed_job(
    tmp_path, remote_dependencies
) -> None:
    context = _remote_context(tmp_path)
    report = _preflight_report(tmp_path, context)
    jobs = FakeJobs()

    result = agy_remote.start_remote(
        "Run the remote task through the approved SSH context.",
        coordinator_dir=str(tmp_path),
        remote_context=context,
        preflight_id=report["preflight_id"],
        execution_options={
            "mode": "accept-edits",
            "approved_commands": ["git status --short"],
            "authorization_ref": "user-approved-remote-1",
        },
        timeout=30,
        jobs=jobs,
    )

    assert report["state"] == "ready_for_probe"
    assert report["evidence_grade"] == "caller_verified"
    assert jobs.started[0]["workdir"] == str(tmp_path)
    assert jobs.started[0]["workdir"] != context["worktree"]
    assert jobs.started[0]["process_control"] == "managed"
    assert result["job_id"] == "remote-job-1"
    assert result["remote_termination"] == "unknown"
    assert result["coordination_evidence"]["evidence_refs"] == context["evidence_refs"]


def test_remote_context_is_bound_to_preflight(
    tmp_path, remote_dependencies
) -> None:
    context = _remote_context(tmp_path)
    report = _preflight_report(tmp_path, context)
    changed = {
        **context,
        "identity": {
            **context["identity"],
            "baseline_sha": "b" * 40,
        },
    }

    with pytest.raises(RemoteStartError, match="identity") as error:
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=changed,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )

    assert error.value.code == "remote_identity_changed"


def test_stale_identity_and_preflight_are_rejected(
    tmp_path, remote_dependencies
) -> None:
    old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    context = _remote_context(
        tmp_path,
        identity={
            **_remote_context(tmp_path)["identity"],
            "observed_at": old,
        },
    )
    report = _preflight_report(tmp_path, context)

    with pytest.raises(RemoteStartError, match="expired|stale"):
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=context,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )


def test_non_development_target_and_credentials_are_rejected(
    tmp_path, remote_dependencies
) -> None:
    production = _remote_context(tmp_path, target_kind="production")
    report = _preflight_report(tmp_path, production)
    with pytest.raises(RemoteStartError) as production_error:
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=production,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )
    assert production_error.value.code == "non_development_target"

    credential_context = _remote_context(tmp_path, private_key="must-not-pass")
    with pytest.raises(RemoteStartError) as credential_error:
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=credential_context,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )
    assert credential_error.value.code == "credentials_forbidden"

    absolute_paths = _remote_context(
        tmp_path,
        owned_paths=["/workspace/all"],
    )
    with pytest.raises(RemoteStartError) as path_error:
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=absolute_paths,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )
    assert path_error.value.code == "invalid_remote_context"


def test_unsupported_remote_lifecycle_is_blocked(
    tmp_path, remote_dependencies
) -> None:
    context = _remote_context(
        tmp_path,
        requires_remote_termination_confirmation=True,
    )
    report = _preflight_report(tmp_path, context)

    with pytest.raises(RemoteStartError) as error:
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=context,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )

    assert error.value.code == "unsupported_remote_lifecycle"


def test_unknown_or_changed_policy_binding_is_blocked(
    tmp_path, remote_dependencies
) -> None:
    context = _remote_context(tmp_path)
    report = _preflight_report(tmp_path, context)

    with pytest.raises(RemoteStartError):
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=context,
            preflight_id="missing-preflight",
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )

    with pytest.raises(RemoteStartError) as changed_options:
        agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=context,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git rev-parse HEAD"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=FakeJobs(),
        )
    assert changed_options.value.code == "policy_binding_changed"


def test_remote_job_status_never_upgrades_remote_termination(
    tmp_path, remote_dependencies, monkeypatch
) -> None:
    context = _remote_context(tmp_path)
    report = _preflight_report(tmp_path, context)
    invocation = ManagedAgyInvocation(
        command=(sys.executable, "-c", "print('fixture')"),
        cwd=str(tmp_path),
        env=dict(os.environ),
        capability_id="cap-remote",
        workdir=str(tmp_path),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.prepare_managed_agy",
        lambda *args, **kwargs: invocation,
    )

    class FakeManaged:
        timeout = 30.0

        def __init__(self, on_started):
            self.on_started = on_started
            self.exit_observed = False

        def run(self):
            self.on_started(6123)
            self.exit_observed = True
            payload = json.dumps({
                "conversation_id": "remote-conversation",
                "status": "SUCCESS",
                "response": "REMOTE_DONE",
                "denied_actions": [],
            })
            return ManagedProcessOutcome(
                stdout=payload,
                stderr="",
                exit_code=0,
                completed=True,
                timed_out=False,
                cancelled=False,
            )

        def request_cancel(self):
            pass

        def termination_evidence(self):
            return {
                "termination_requested": False,
                "termination_confirmed": False,
                "local_termination": "not_requested",
                "remote_termination": "unknown",
                "process_exit_observed": self.exit_observed,
                "tree_kill_attempted": False,
                "tree_kill_succeeded": False,
                "job_object_assigned": True,
                "pid": 6123,
            }

    registry = AgyJobRegistry(
        managed_factory=lambda invocation, on_started: FakeManaged(on_started)
    )
    try:
        result = agy_remote.start_remote(
            "Run task",
            coordinator_dir=str(tmp_path),
            remote_context=context,
            preflight_id=report["preflight_id"],
            execution_options={
                "mode": "accept-edits",
                "approved_commands": ["git status --short"],
                "authorization_ref": "user-approved-remote-1",
            },
            timeout=30,
            jobs=registry,
        )
        deadline = time.monotonic() + 2
        while registry.status(result["job_id"])["state"] in {"queued", "running"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        status = registry.status(result["job_id"])
    finally:
        registry.close()

    assert status["state"] == "completed"
    assert status["process_control"] == "managed"
    assert status["remote_termination"] == "unknown"
    assert status["remote_context_digest"] == result["remote_context_digest"]
    assert status["business_acceptance"] == "not_started"


def test_server_remote_tool_returns_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "start_remote",
        lambda *args, **kwargs: {
            "job_id": "remote-job-1",
            "remote_termination": "unknown",
        },
    )

    payload = json.loads(server.agy_remote_start(
        "Run task",
        str(tmp_path),
        _remote_context(tmp_path),
        "preflight-1",
        timeout=30,
    ))

    assert payload["job_id"] == "remote-job-1"
    assert payload["remote_termination"] == "unknown"
