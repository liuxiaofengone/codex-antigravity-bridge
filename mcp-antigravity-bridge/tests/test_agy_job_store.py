"""Private persisted job metadata tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from codex_agy_bridge.agy_job_store import JobStore


def _record(job_id: str = "job-1") -> dict:
    return {
        "job_id": job_id,
        "state": "running",
        "process_control": "managed",
        "cancellation_supported": True,
        "created_at": "2026-11-06T00:00:00+00:00",
        "updated_at": "2026-11-06T00:00:01+00:00",
        "workdir": "C:\\work",
        "pid": 1234,
        "exit_code": None,
        "error_kind": None,
        "reason": None,
        "remote_termination": "unknown",
    }


def test_job_store_round_trip_without_prompt_or_raw_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    store = JobStore()
    record = {
        **_record(),
        "prompt": "must not persist",
        "text": "raw output must not persist",
        "stderr": "raw error must not persist",
        "failed_command": "curl -H 'Authorization: Bearer secret' example.test",
        "permission_resource": "secret-permission-resource",
    }

    store.save(record)
    loaded = store.load("job-1")

    assert loaded["job_id"] == "job-1"
    assert loaded["process_control"] == "managed"
    assert loaded["pid"] == 1234
    assert "prompt" not in loaded
    assert "text" not in loaded
    assert "stderr" not in loaded
    assert "failed_command" not in loaded
    assert "permission_resource" not in loaded
    stored_text = (tmp_path / "state" / "jobs" / "job-1.json").read_text(
        encoding="utf-8"
    )
    assert "must not persist" not in stored_text
    assert "raw output" not in stored_text
    assert "secret-permission-resource" not in stored_text


def test_job_store_missing_and_corrupt_records_are_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    store = JobStore()

    assert store.load("missing") is None

    job_dir = tmp_path / "jobs"
    job_dir.mkdir()
    (job_dir / "broken.json").write_text("{broken", encoding="utf-8")
    assert store.load("broken") is None


def test_job_store_rejects_unsafe_ids(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    store = JobStore()

    for job_id in ("../escape", "a/b", ""):
        assert store.load(job_id) is None


def test_job_record_has_private_permissions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    store = JobStore()

    store.save(_record())
    path = tmp_path / "jobs" / "job-1.json"

    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["remote_termination"] == "unknown"
