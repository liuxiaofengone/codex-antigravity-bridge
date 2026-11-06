"""Policy evidence persistence, freshness, and scope tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_agy_bridge.agy_capabilities import CapabilitySnapshot
from codex_agy_bridge.agy_evidence import (
    BRIDGE_VERSION,
    POLICY_REQUIRED_CHECKS,
    PolicyEvidence,
    PolicyEvidenceError,
    load_policy_evidence,
    load_verified_policy_evidence,
    save_policy_evidence,
    verify_policy_evidence,
)


def _capability(capability_id: str = "cap-1", digest: str = "digest-1") -> CapabilitySnapshot:
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
        settings_summary={"digest": digest},
    )


def _evidence(**overrides) -> PolicyEvidence:
    now = datetime.now(timezone.utc)
    values = {
        "evidence_id": "evidence-1",
        "cli_fingerprint": "cap-1",
        "base_policy_digest": "digest-1",
        "platform": "Windows-ARM64-Python 3.14.10",
        "builder_version": BRIDGE_VERSION,
        "tested_modes": ("accept-edits",),
        "selector_shapes": ("exact_command",),
        "checks": {name: True for name in POLICY_REQUIRED_CHECKS},
        "verified_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
    }
    values.update(overrides)
    return PolicyEvidence(**values)


def test_evidence_round_trip_is_private(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    evidence = _evidence()

    save_policy_evidence(evidence)
    loaded = load_policy_evidence("evidence-1")

    assert loaded == evidence
    state_file = tmp_path / "state" / "policy-evidence.json"
    assert state_file.is_file()
    assert "another-secret" not in state_file.read_text(encoding="utf-8")


def test_valid_evidence_is_accepted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    evidence = _evidence()
    save_policy_evidence(evidence)

    loaded = load_verified_policy_evidence(
        _capability(), "accept-edits", ("exact_command",)
    )

    assert loaded.evidence_id == evidence.evidence_id


@pytest.mark.parametrize(
    "capability,mode,selectors",
    [
        (_capability(capability_id="changed"), "accept-edits", ("exact_command",)),
        (_capability(digest="changed"), "accept-edits", ("exact_command",)),
        (_capability(), "auto", ("exact_command",)),
        (_capability(), "accept-edits", ("prefix_command",)),
    ],
)
def test_scope_mismatch_is_policy_unverified(
    capability, mode, selectors
) -> None:
    with pytest.raises(PolicyEvidenceError, match="policy_unverified"):
        verify_policy_evidence(_evidence(), capability, mode, selectors)


def test_expired_or_missing_check_is_policy_unverified() -> None:
    expired = _evidence(
        verified_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    missing_check = _evidence(
        checks={POLICY_REQUIRED_CHECKS[0]: True},
    )

    with pytest.raises(PolicyEvidenceError, match="policy_unverified"):
        verify_policy_evidence(
            expired, _capability(), "accept-edits", ("exact_command",)
        )
    with pytest.raises(PolicyEvidenceError, match="policy_unverified"):
        verify_policy_evidence(
            missing_check, _capability(), "accept-edits", ("exact_command",)
        )


def test_corrupt_or_missing_store_is_policy_unverified(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "policy-evidence.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(state))

    with pytest.raises(PolicyEvidenceError, match="policy_unverified"):
        load_verified_policy_evidence(
            _capability(), "accept-edits", ("exact_command",)
        )
    (state / "policy-evidence.json").unlink()
    with pytest.raises(PolicyEvidenceError, match="policy_unverified"):
        load_verified_policy_evidence(
            _capability(), "accept-edits", ("exact_command",)
        )


def test_tampered_check_values_and_extended_ttl_are_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    evidence = _evidence(
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
    )
    save_policy_evidence(evidence)
    path = tmp_path / "policy-evidence.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["checks"][POLICY_REQUIRED_CHECKS[0]] = "true"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PolicyEvidenceError, match="policy_unverified"):
        load_verified_policy_evidence(
            _capability(), "accept-edits", ("exact_command",)
        )
