"""Execution option validation and session-settings construction tests."""

from __future__ import annotations

import json
import subprocess
import os
import re
from types import SimpleNamespace

import pytest

from codex_agy_bridge import agy_policy, agy_runner
from codex_agy_bridge.agy_capabilities import CapabilitySnapshot
from codex_agy_bridge.agy_policy import ExecutionOptions, PolicyError


def _capabilities(settings: bool = True) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        capability_id="capability-1",
        binary_path="C:\\agy.exe",
        cli_version="1.5.1",
        python_platform="Windows-ARM64-Python 3.14.10",
        declared_flags={"--settings": settings, "--mode": settings},
        mode_availability={
            "accept-edits": "unknown",
            "auto": "unknown",
            "plan": "unknown",
        },
        settings_summary={"digest": "base-policy"},
    )


def _evidence() -> SimpleNamespace:
    return SimpleNamespace(
        evidence_id="evidence-1",
        checks={name: True for name in agy_policy.POLICY_REQUIRED_CHECKS},
        tested_modes=("accept-edits", "auto", "plan"),
        selector_shapes=("exact_command",),
    )


def test_default_options_preserve_legacy_argv() -> None:
    options = agy_policy.parse_execution_options(None)

    assert options == ExecutionOptions()
    assert agy_policy.build_session_args(options, _capabilities(), {}) == []


def test_nonempty_options_require_authorization_reference() -> None:
    with pytest.raises(PolicyError, match="authorization_ref") as error:
        agy_policy.parse_execution_options({
            "mode": "accept-edits",
            "approved_commands": ["git status --short"],
        })

    assert error.value.code == "authorization_required"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(PolicyError, match="mode") as error:
        agy_policy.parse_execution_options({
            "mode": "danger-full-access",
            "authorization_ref": "user-approved-1",
        })

    assert error.value.code == "invalid_options"


def test_old_cli_without_settings_is_unsupported() -> None:
    options = ExecutionOptions(
        mode="accept-edits",
        approved_commands=("git status --short",),
        authorization_ref="user-approved-1",
    )

    with pytest.raises(PolicyError, match="--settings") as error:
        agy_policy.build_session_args(options, _capabilities(settings=False), {})

    assert error.value.code == "unsupported_capability"


def test_skip_permissions_conflicts_with_nonempty_options() -> None:
    with pytest.raises(PolicyError, match="conflict") as error:
        agy_policy.parse_execution_options(
            {
                "mode": "accept-edits",
                "authorization_ref": "user-approved-1",
            },
            dangerously_skip_permissions=True,
        )

    assert error.value.code == "parameter_conflict"


def test_exact_commands_are_used_instead_of_broad_prefixes(monkeypatch) -> None:
    monkeypatch.setattr(
        agy_policy,
        "load_verified_policy_evidence",
        lambda *args, **kwargs: _evidence(),
    )
    options = ExecutionOptions(
        mode="accept-edits",
        approved_commands=("git status --short", "git rev-parse HEAD"),
        authorization_ref="user-approved-1",
    )
    inherited = {
        "permissions": {
            "allow": ["command(Get-ChildItem)"],
            "ask": ["command(ssh *)"],
            "deny": ["command(Remove-Item -Recurse *)"],
        }
    }

    args = agy_policy.build_session_args(options, _capabilities(), inherited)
    settings_text = args[args.index("--settings") + 1]
    settings = json.loads(settings_text)
    allow = settings["permissions"]["allow"]

    assert "command(git status --short)" in allow
    assert "command(git rev-parse HEAD)" in allow
    assert "command(git)" not in allow
    assert "command(cnb)" not in allow
    assert settings["permissions"]["ask"] == inherited["permissions"]["ask"]
    assert settings["permissions"]["deny"] == inherited["permissions"]["deny"]
    assert args[-2:] == ["--mode", "accept-edits"]


def test_no_evidence_blocks_nonempty_options(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise agy_policy.PolicyEvidenceError("no usable policy evidence")

    monkeypatch.setattr(agy_policy, "load_verified_policy_evidence", fail)
    options = ExecutionOptions(
        mode="accept-edits",
        approved_commands=("git status --short",),
        authorization_ref="user-approved-1",
    )

    with pytest.raises(PolicyError, match="policy_unverified") as error:
        agy_policy.build_session_args(options, _capabilities(), {})

    assert error.value.code == "policy_unverified"


def test_builder_revalidates_manually_constructed_options(monkeypatch) -> None:
    monkeypatch.setattr(
        agy_policy,
        "load_verified_policy_evidence",
        lambda *args, **kwargs: _evidence(),
    )
    options = ExecutionOptions(
        mode="accept-edits",
        approved_commands=("git status --short",),
    )

    with pytest.raises(PolicyError, match="authorization_ref") as error:
        agy_policy.build_session_args(options, _capabilities(), {})

    assert error.value.code == "authorization_required"


def test_approved_command_with_control_characters_is_rejected() -> None:
    with pytest.raises(PolicyError, match="control") as error:
        agy_policy.parse_execution_options({
            "approved_commands": ["git status --short\nwhoami"],
            "authorization_ref": "user-approved-1",
        })

    assert error.value.code == "invalid_options"


def test_runner_appends_verified_session_args(monkeypatch) -> None:
    captured = {}
    options = ExecutionOptions(
        mode="accept-edits",
        approved_commands=("git status --short",),
        authorization_ref="user-approved-1",
    )
    monkeypatch.setattr(agy_runner, "find_agy", lambda: "agy")
    monkeypatch.setattr(agy_runner, "load_inherited_permissions", lambda: {})
    monkeypatch.setattr(
        agy_runner,
        "inspect_agy_capabilities",
        lambda refresh=False: _capabilities(),
    )
    monkeypatch.setattr(
        agy_runner,
        "build_session_args",
        lambda *args, **kwargs: ["--settings", "{\"permissions\":{}}"],
    )

    def fake_run(args, workdir, timeout, env=None):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "OK", "")

    monkeypatch.setattr(agy_runner, "_run_subprocess", fake_run)

    result = agy_runner.run_agy("Say hi", execution_options=options)

    assert result.text == "OK"
    assert captured["args"][:3] == ["agy", "-p", "Say hi"]
    assert captured["args"][3:] == ["--settings", "{\"permissions\":{}}"]


def test_stream_step_update_can_prove_tool_execution() -> None:
    command = (
        "powershell.exe -NoProfile -NonInteractive -Command "
        "\"Write-Output 'AGY_POLICY_CANARY_TEST'\""
    )
    marker = "AGY_POLICY_CANARY_TEST"
    output = json.dumps({
        "type": "step_update",
        "step_type": "tool_call",
        "tool_name": "RunCommand",
        "input": {"command": command},
        "output": marker,
    })

    assert agy_policy._tool_event_confirms(output, marker, command) is True


def test_real_stream_step_shape_requires_done_and_command_output() -> None:
    command = (
        "powershell.exe -NoProfile -NonInteractive -Command "
        "\"Write-Output 'AGY_POLICY_CANARY_TEST'\""
    )
    marker = "AGY_POLICY_CANARY_TEST"
    done = json.dumps({
        "event": "step_update",
        "step_update": {
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {
                "name": "run_command",
                "parameters": {"CommandLine": command},
                "output": marker,
            },
        },
    })
    attempted_without_output = json.dumps({
        "event": "step_update",
        "step_update": {
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {
                "name": "run_command",
                "parameters": {"CommandLine": command},
            },
        },
    })

    assert agy_policy._tool_event_confirms(done, marker, command) is True
    assert agy_policy._tool_event_confirms(
        attempted_without_output, marker, command
    ) is False
    summary = agy_policy._event_summary(done, marker, command)
    assert summary["command_present"] is True
    assert summary["tool_execution_confirmed"] is True


def test_real_tool_error_marks_permission_denial() -> None:
    output = json.dumps({
        "event": "step_update",
        "step_update": {
            "state": "ERROR",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {
                "name": "run_command",
                "parameters": {
                    "CommandLine": (
                        "powershell.exe -NoProfile -NonInteractive -Command "
                        "\"Write-Output 'test'\""
                    )
                },
                "error": "Permission denied. Matches user-configured deny rule.",
            },
        },
    })

    assert agy_policy._event_was_denied(output) is True


def test_policy_probe_does_not_accept_model_self_report(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGY_POLICY_PROBE_OPT_IN", "1")
    monkeypatch.setattr(
        "codex_agy_bridge.agy_capabilities.inspect_agy_capabilities",
        lambda refresh=False: _capabilities(),
    )
    monkeypatch.setattr("codex_agy_bridge.agy_runner.find_agy", lambda: "agy")
    monkeypatch.setattr(agy_policy, "load_inherited_permissions", lambda: {
        "permissions": {"allow": []},
    })
    monkeypatch.setattr(
        agy_policy,
        "_run_canary_call",
        lambda *args, **kwargs: json.dumps({
            "type": "message",
            "content": "AGY_POLICY_CANARY_FAKE ran successfully; Bearer super-secret",
        }),
    )

    result = agy_policy.probe_agy_policy("user-approved-1", timeout=1)

    assert result["state"] == "policy_unverified", result["checks"]
    assert result["diagnostics"]["session_a"]["event_types"] == ["message"]
    assert result["diagnostics"]["session_a"]["top_level_keys"] == ["content", "type"]
    assert result["diagnostics"]["session_a"]["type_values"] == ["message"]
    assert result["diagnostics"]["session_a"]["tool_shapes"] == []
    assert "super-secret" not in json.dumps(result)


def test_policy_probe_requires_observed_unapproved_block(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AGY_POLICY_PROBE_OPT_IN", "1")
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        "codex_agy_bridge.agy_capabilities.inspect_agy_capabilities",
        lambda refresh=False: _capabilities(),
    )
    monkeypatch.setattr("codex_agy_bridge.agy_runner.find_agy", lambda: "agy")
    monkeypatch.setattr(agy_policy, "load_inherited_permissions", lambda: {
        "permissions": {"allow": []},
    })
    calls = {"count": 0}

    def fake_call(binary, prompt, workdir, timeout, args):
        calls["count"] += 1
        command = re.search(r"Run exactly `([^`]+)`", prompt).group(1)
        marker = re.search(r"AGY_POLICY_CANARY_[A-F0-9]+", command).group(0)
        if calls["count"] == 1:
            return json.dumps({
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": command},
                        "output": marker,
                    },
                },
            })
        if calls["count"] == 2:
            return json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "not executed",
                    "denied_actions": [],
                },
            })
        return json.dumps({
            "event": "step_update",
            "step_update": {
                "state": "ERROR",
                "step_type": "tool",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": command},
                    "error": "Permission denied: deny rule",
                },
            },
        })

    monkeypatch.setattr(agy_policy, "_run_canary_call", fake_call)

    result = agy_policy.probe_agy_policy("user-approved-1", timeout=1)

    assert result["state"] == "policy_unverified", result["checks"]
    assert result["checks"]["unapproved_command_blocked"] is False
