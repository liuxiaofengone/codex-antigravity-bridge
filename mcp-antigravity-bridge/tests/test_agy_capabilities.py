"""Capability discovery tests that never invoke an AGY model prompt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_agy_bridge import agy_capabilities, server


HELP_TEXT = """\
Usage of agy.exe:
  --mode        Set the agent execution mode
  --output-format Output format for print mode
  -p            Run a single prompt non-interactively
  --print-timeout Optional time limit for print mode
  --input-format Input format for print mode
  --json-schema Optional JSON schema string
  --remote-agent Run this session's conversations using remote agents
  --remote-control Create a remote connection on startup
  --settings    Settings for this session only, never saved
"""


def _fake_probe(calls: list[list[str]] | None = None):
    calls = calls if calls is not None else []

    def probe(args: list[str], timeout: float) -> SimpleNamespace:
        calls.append(list(args))
        if args[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="1.5.1\n", stderr="")
        if args[-1] == "--help":
            return SimpleNamespace(returncode=0, stdout=HELP_TEXT, stderr="")
        raise AssertionError(f"unexpected probe: {args}")

    return probe


def test_help_declaration_is_not_runtime_availability(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"fake-binary")
    settings_root = tmp_path / "settings"
    settings_root.mkdir()
    (settings_root / "user_settings.pb").write_bytes(b"opaque-settings")
    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.setenv("AGY_SETTINGS_ROOT", str(settings_root))
    monkeypatch.setattr(agy_capabilities, "_run_probe", _fake_probe())
    agy_capabilities.clear_capability_cache()

    snapshot = agy_capabilities.inspect_agy_capabilities(refresh=True)

    assert snapshot.declared_flags["--settings"] is True
    assert snapshot.declared_flags["--remote-agent"] is True
    assert snapshot.declared_flags["--input-format"] is True
    assert snapshot.declared_flags["--json-schema"] is True
    assert snapshot.declared_flags["--remote-control"] is True
    assert snapshot.mode_availability == {
        "accept-edits": "unknown",
        "auto": "unknown",
        "plan": "unknown",
    }


def test_help_output_on_stderr_is_recognized(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"fake-binary")

    def probe(args: list[str], timeout: float) -> SimpleNamespace:
        if args[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="1.5.1\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr=HELP_TEXT)

    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.setenv("AGY_SETTINGS_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(agy_capabilities, "_run_probe", probe)
    agy_capabilities.clear_capability_cache()

    snapshot = agy_capabilities.inspect_agy_capabilities(refresh=True)

    assert snapshot.declared_flags["--settings"] is True


def test_capability_id_changes_with_binary_and_settings_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"one")
    settings_root = tmp_path / "settings"
    settings_root.mkdir()
    settings = settings_root / "user_settings.pb"
    settings.write_bytes(b"first")
    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.setenv("AGY_SETTINGS_ROOT", str(settings_root))
    monkeypatch.setattr(agy_capabilities, "_run_probe", _fake_probe())
    agy_capabilities.clear_capability_cache()

    first = agy_capabilities.inspect_agy_capabilities(refresh=True)
    binary.write_bytes(b"two")
    second = agy_capabilities.inspect_agy_capabilities(refresh=True)
    settings.write_bytes(b"second")
    third = agy_capabilities.inspect_agy_capabilities(refresh=True)

    assert first.capability_id != second.capability_id
    assert second.capability_id != third.capability_id


def test_capability_id_tracks_shared_permission_policy(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"fake-binary")
    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.delenv("AGY_SETTINGS_ROOT", raising=False)
    monkeypatch.setattr(agy_capabilities, "_home_dir", lambda: tmp_path)
    monkeypatch.setattr(agy_capabilities, "_run_probe", _fake_probe())
    policy = tmp_path / ".gemini" / "config" / "config.json"
    policy.parent.mkdir(parents=True)
    policy.write_text('{"permissions":{"allow":["git status"]}}', encoding="utf-8")
    agy_capabilities.clear_capability_cache()

    before = agy_capabilities.inspect_agy_capabilities(refresh=True)
    policy.write_text('{"permissions":{"allow":["git status --short"]}}', encoding="utf-8")
    after = agy_capabilities.inspect_agy_capabilities(refresh=True)

    assert before.capability_id != after.capability_id
    assert any(
        source["path"] == str(policy)
        for source in after.settings_summary["sources"]
    )


def test_refresh_false_reuses_observation(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"fake-binary")
    calls: list[list[str]] = []
    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.setenv("AGY_SETTINGS_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(agy_capabilities, "_run_probe", _fake_probe(calls))
    agy_capabilities.clear_capability_cache()

    first = agy_capabilities.inspect_agy_capabilities(refresh=True)
    second = agy_capabilities.inspect_agy_capabilities(refresh=False)

    assert first == second
    assert len(calls) == 2


def test_missing_binary_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(agy_capabilities, "find_agy", lambda: None)
    agy_capabilities.clear_capability_cache()

    with pytest.raises(agy_capabilities.CapabilityDiscoveryError, match="not found"):
        agy_capabilities.inspect_agy_capabilities(refresh=True)


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=1, stdout="", stderr="help failed"),
        SimpleNamespace(returncode=0, stdout="not an agy help page", stderr=""),
    ],
)
def test_help_failure_or_unknown_format_is_explicit(
    tmp_path: Path, monkeypatch, result
) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"fake-binary")
    seen: list[list[str]] = []

    def probe(args: list[str], timeout: float):
        seen.append(list(args))
        if args[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="1.5.1", stderr="")
        return result

    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.setenv("AGY_SETTINGS_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(agy_capabilities, "_run_probe", probe)
    agy_capabilities.clear_capability_cache()

    with pytest.raises(agy_capabilities.CapabilityDiscoveryError, match="help"):
        agy_capabilities.inspect_agy_capabilities(refresh=True)

    assert all("-p" not in args for args in seen)


def test_snapshot_redacts_opaque_settings(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"fake-binary")
    settings_root = tmp_path / "settings"
    settings_root.mkdir()
    secret = "token=do-not-print-this"
    settings = settings_root / "user_settings.pb"
    settings.write_text(secret, encoding="utf-8")
    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.setenv("AGY_SETTINGS_ROOT", str(settings_root))
    monkeypatch.setattr(agy_capabilities, "_run_probe", _fake_probe())
    agy_capabilities.clear_capability_cache()

    snapshot = agy_capabilities.inspect_agy_capabilities(refresh=True)
    rendered = json.dumps(snapshot.as_dict(), ensure_ascii=False)

    assert secret not in rendered
    assert settings.name in rendered
    expected_digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert expected_digest in rendered


def test_doctor_never_starts_a_prompt(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_bytes(b"fake-binary")
    calls: list[list[str]] = []
    monkeypatch.setenv("AGY_PATH", str(binary))
    monkeypatch.setenv("AGY_SETTINGS_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(agy_capabilities, "_run_probe", _fake_probe(calls))
    monkeypatch.setattr(server, "inspect_agy_capabilities",
                        agy_capabilities.inspect_agy_capabilities)
    agy_capabilities.clear_capability_cache()

    payload = json.loads(server.agy_doctor(refresh=True))

    assert payload["cli_version"] == "1.5.1"
    assert payload["declared_flags"]["--settings"] is True
    assert all("-p" not in args and "--print" not in args for args in calls)
