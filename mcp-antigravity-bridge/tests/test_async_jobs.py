from __future__ import annotations

import time

import pytest

from codex_agy_bridge.agy_jobs import AgyJobRegistry
from codex_agy_bridge.agy_runner import AgyResult


def test_start_and_status_reports_completed_job(monkeypatch):
    def fake_run_agy(*args, **kwargs):
        return AgyResult(text="DONE", exit_code=0, used_pty=False)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", fake_run_agy)
    registry = AgyJobRegistry()

    job_id = registry.start("Implement page", workdir="C:\\work")
    for _ in range(20):
        status = registry.status(job_id)
        if status["state"] == "completed":
            break
        time.sleep(0.01)

    assert status == {
        "job_id": job_id,
        "state": "completed",
        "text": "DONE",
        "exit_code": 0,
        "used_pty": False,
    }


def test_status_reports_running_job(monkeypatch):
    def slow_run_agy(*args, **kwargs):
        time.sleep(0.1)
        return AgyResult(text="DONE", exit_code=0, used_pty=False)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", slow_run_agy)
    registry = AgyJobRegistry()

    job_id = registry.start("Implement page")
    status = registry.status(job_id)

    assert status["job_id"] == job_id
    assert status["state"] in {"queued", "running"}


def test_status_reports_unknown_job():
    registry = AgyJobRegistry()

    assert registry.status("missing") == {
        "job_id": "missing",
        "state": "unknown",
        "error": "job not found",
    }


def test_status_reports_nonzero_exit_as_failed(monkeypatch):
    def failed_run_agy(*args, **kwargs):
        return AgyResult(text="agy failed", exit_code=1, used_pty=False)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", failed_run_agy)
    registry = AgyJobRegistry()

    job_id = registry.start("Fail this task")
    for _ in range(20):
        status = registry.status(job_id)
        if status["state"] == "failed":
            break
        time.sleep(0.01)

    assert status["job_id"] == job_id
    assert status["state"] == "failed"
    assert status["text"] == "agy failed"
    assert status["exit_code"] == 1
    assert status["used_pty"] is False
    assert status["error_kind"] == "unknown"
    assert status["error"].startswith("AGY_FAILED: agy exited with code 1: agy failed")
    assert status["reason"] == "exit_code_1"


def test_status_reports_login_required_for_authentication_failure(monkeypatch):
    def failed_run_agy(*args, **kwargs):
        return AgyResult(text="authentication required", exit_code=1, used_pty=False)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", failed_run_agy)
    registry = AgyJobRegistry()
    try:
        job_id = registry.start("Login test")
        for _ in range(20):
            status = registry.status(job_id)
            if status["state"] == "failed":
                break
            time.sleep(0.01)

        assert status["error_kind"] == "authentication"
        assert status["error"].startswith("AGY_LOGIN_REQUIRED:")
    finally:
        registry.close()


def test_completed_jobs_are_pruned_after_retention(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.run_agy",
        lambda *args, **kwargs: AgyResult(text="DONE", exit_code=0, used_pty=False),
    )
    registry = AgyJobRegistry(retention_seconds=0.05)
    try:
        job_id = registry.start("Implement page")
        for _ in range(20):
            if registry.status(job_id)["state"] == "completed":
                break
            time.sleep(0.01)
        time.sleep(0.1)
        assert registry.cleanup() == 1
        assert registry.status(job_id)["state"] == "unknown"
    finally:
        registry.close()


def test_closed_registry_rejects_new_jobs(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.run_agy",
        lambda *args, **kwargs: AgyResult(text="DONE", exit_code=0, used_pty=False),
    )
    registry = AgyJobRegistry()
    registry.close()

    with pytest.raises(RuntimeError, match="closed"):
        registry.start("Implement page")


def test_terminal_display_selects_visible_runner(monkeypatch):
    calls = []

    def fake_visible_run(*args, **kwargs):
        calls.append((args, kwargs))
        return AgyResult(text="shown", exit_code=0, used_pty=False)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy_visible", fake_visible_run)
    registry = AgyJobRegistry()
    try:
        job_id = registry.start("Show task", display_mode="terminal")
        for _ in range(20):
            status = registry.status(job_id)
            if status["state"] == "completed":
                break
            time.sleep(0.01)
        assert status["text"] == "shown"
        assert calls
    finally:
        registry.close()
