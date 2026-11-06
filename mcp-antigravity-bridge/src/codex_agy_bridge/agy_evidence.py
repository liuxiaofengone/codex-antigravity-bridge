"""Private, expiring evidence for AGY session-settings semantics."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .agy_capabilities import CapabilitySnapshot

BRIDGE_VERSION = "0.2.0"
POLICY_REQUIRED_CHECKS = (
    "approved_command_executed",
    "unapproved_command_blocked",
    "deny_ask_preserved",
    "global_settings_unchanged",
    "session_isolation",
)
_EVIDENCE_FILE = "policy-evidence.json"


class PolicyEvidenceError(RuntimeError):
    """Raised when policy evidence is absent, stale, corrupt, or out of scope."""


@dataclass(frozen=True)
class PolicyEvidence:
    evidence_id: str
    cli_fingerprint: str
    base_policy_digest: str
    platform: str
    builder_version: str
    tested_modes: tuple[str, ...]
    selector_shapes: tuple[str, ...]
    checks: dict[str, bool]
    verified_at: str
    expires_at: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "PolicyEvidence":
        return cls(
            evidence_id=str(raw["evidence_id"]),
            cli_fingerprint=str(raw["cli_fingerprint"]),
            base_policy_digest=str(raw["base_policy_digest"]),
            platform=str(raw["platform"]),
            builder_version=str(raw["builder_version"]),
            tested_modes=tuple(str(item) for item in raw["tested_modes"]),
            selector_shapes=tuple(str(item) for item in raw["selector_shapes"]),
            checks={
                str(key): value is True
                for key, value in dict(raw["checks"]).items()
            },
            verified_at=str(raw["verified_at"]),
            expires_at=str(raw["expires_at"]),
        )

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tested_modes"] = list(self.tested_modes)
        data["selector_shapes"] = list(self.selector_shapes)
        return data


def _state_dir() -> Path:
    configured = os.environ.get("AGY_BRIDGE_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex-agy-bridge"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def save_policy_evidence(evidence: PolicyEvidence) -> None:
    """Write one evidence record atomically in the private bridge state dir."""
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    path = directory / _EVIDENCE_FILE
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".policy-evidence-",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(evidence.as_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        try:
            temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def load_policy_evidence(evidence_id: str | None = None) -> PolicyEvidence:
    path = _state_dir() / _EVIDENCE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        evidence = PolicyEvidence.from_mapping(raw)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PolicyEvidenceError(
            f"policy_unverified: evidence is unavailable or corrupt ({type(exc).__name__})"
        ) from exc
    if evidence_id is not None and evidence.evidence_id != evidence_id:
        raise PolicyEvidenceError("policy_unverified: evidence id does not match")
    return evidence


def verify_policy_evidence(
    evidence: PolicyEvidence,
    capability: CapabilitySnapshot,
    mode: str,
    selector_shapes: tuple[str, ...],
) -> None:
    """Raise `policy_unverified` unless evidence covers the live invocation."""
    now = datetime.now(timezone.utc)
    mismatches: list[str] = []
    try:
        verified_at = _parse_time(evidence.verified_at)
        expires_at = _parse_time(evidence.expires_at)
    except ValueError:
        mismatches.append("timestamp")
    else:
        if verified_at > now or expires_at <= now:
            mismatches.append("expired")
        if expires_at - verified_at > timedelta(hours=24):
            mismatches.append("ttl")
    if evidence.cli_fingerprint != capability.capability_id:
        mismatches.append("cli")
    if evidence.base_policy_digest != capability.settings_summary.get("digest"):
        mismatches.append("base_policy")
    if evidence.platform != capability.python_platform:
        mismatches.append("platform")
    if evidence.builder_version != BRIDGE_VERSION:
        mismatches.append("builder_version")
    if mode not in evidence.tested_modes:
        mismatches.append("mode")
    if not set(selector_shapes).issubset(evidence.selector_shapes):
        mismatches.append("selector_shape")
    for name in POLICY_REQUIRED_CHECKS:
        if evidence.checks.get(name) is not True:
            mismatches.append(f"check:{name}")
    if mismatches:
        raise PolicyEvidenceError(
            "policy_unverified: " + ", ".join(dict.fromkeys(mismatches))
        )


def load_verified_policy_evidence(
    capability: CapabilitySnapshot,
    mode: str,
    selector_shapes: tuple[str, ...],
) -> PolicyEvidence:
    evidence = load_policy_evidence()
    verify_policy_evidence(evidence, capability, mode, selector_shapes)
    return evidence
