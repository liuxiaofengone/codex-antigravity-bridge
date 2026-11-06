from __future__ import annotations

import json
import time
import os
import sys
from threading import Event

import pytest

from codex_agy_bridge.agy_job_store import JobStore
from codex_agy_bridge.agy_jobs import AgyJobRegistry
from codex_agy_bridge.agy_policy import ExecutionOptions
from codex_agy_bridge.agy_process import ManagedProcessOutcome
from codex_agy_bridge.agy_runner import (
    AgyResult,
    ManagedAgyInvocation,
    prepare_managed_agy,
)


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

    assert status["job_id"] == job_id
    assert status["state"] == "completed"
    assert status["text"] == "DONE"
    assert status["exit_code"] == 0
    assert status["used_pty"] is False
    assert status["process_control"] == "legacy"
    assert status["cancellation_support"] == "queued_only"


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

    status = registry.status("missing")
    assert status["job_id"] == "missing"
    assert status["state"] == "unknown"
    assert status["error"] == "job not found"
    assert status["process_control"] == "unknown"


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


def test_completed_jobs_are_pruned_after_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.run_agy",
        lambda *args, **kwargs: AgyResult(text="DONE", exit_code=0, used_pty=False),
    )
    registry = AgyJobRegistry(retention_seconds=0.05, store=JobStore())
    try:
        job_id = registry.start("Implement page")
        for _ in range(20):
            if registry.status(job_id)["state"] == "completed":
                break
            time.sleep(0.01)
        time.sleep(0.1)
        assert registry.cleanup() == 1
        recovered = registry.status(job_id)
        assert recovered["state"] == "completed"
        assert recovered["restart_recovered"] is True
        assert recovered["cancellation_support"] == "none"
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


def test_registry_forwards_execution_options(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return AgyResult(text="DONE", exit_code=0)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", fake_run)
    options = ExecutionOptions(
        mode="accept-edits",
        approved_commands=("git status --short",),
        authorization_ref="user-approved-1",
    )
    registry = AgyJobRegistry()
    try:
        job_id = registry.start("Implement", execution_options=options)
        for _ in range(20):
            if registry.status(job_id)["state"] == "completed":
                break
            time.sleep(0.01)
        assert captured["execution_options"] == options
    finally:
        registry.close()


def test_managed_terminal_mode_is_rejected() -> None:
    registry = AgyJobRegistry()
    try:
        with pytest.raises(ValueError, match="managed.*headless|terminal.*managed"):
            registry.start(
                "Implement",
                display_mode="terminal",
                process_control="managed",
            )
    finally:
        registry.close()


def test_managed_invocation_uses_one_stream_json_command(monkeypatch):
    captured = {}

    def fake_environment(force=False):
        captured["force"] = force
        return {"HTTP_PROXY": "http://127.0.0.1:7890"}

    monkeypatch.setattr("codex_agy_bridge.agy_runner.find_agy", lambda: "agy.exe")
    monkeypatch.setattr(
        "codex_agy_bridge.agy_runner.resolve_agy_environment",
        fake_environment,
    )

    invocation = prepare_managed_agy("Inspect repository")

    assert invocation.command[0] == "agy.exe"
    assert "--output-format" in invocation.command
    assert invocation.command[
        invocation.command.index("--output-format") + 1
    ] == "stream-json"
    assert "--input-format" not in invocation.command
    assert invocation.command.count("-p") == 1
    assert "cmd" not in invocation.command[0].lower()


def test_legacy_running_cancel_is_not_reported_as_terminated(monkeypatch):
    started = Event()
    release = Event()

    def blocking_run(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return AgyResult(text="DONE", exit_code=0)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", blocking_run)
    registry = AgyJobRegistry()
    try:
        job_id = registry.start("Implement")
        assert started.wait(timeout=2)
        status = registry.status(job_id)
        assert status["state"] == "running"
        assert status["cancellation_support"] == "queued_only"

        report = registry.cancel(job_id)

        assert report["state"] == "unsupported_running_legacy"
        assert report["cancel_requested"] is False
        assert report["termination_confirmed"] is False
        assert registry.status(job_id)["state"] == "running"
        release.set()
    finally:
        release.set()
        registry.close()


def test_legacy_queued_cancel_requires_future_cancel(monkeypatch):
    first_started = Event()
    release = Event()
    calls = {"count": 0}

    def blocking_run(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            first_started.set()
            release.wait(timeout=5)
        return AgyResult(text="DONE", exit_code=0)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", blocking_run)
    registry = AgyJobRegistry(max_workers=1)
    try:
        first = registry.start("First")
        assert first_started.wait(timeout=2)
        queued = registry.start("Queued")
        assert registry.status(queued)["state"] == "queued"

        report = registry.cancel(queued)

        assert report["state"] == "cancelled"
        assert report["queued_cancelled"] is True
        assert report["local_termination"] == "not_started"
        assert registry.status(queued)["state"] == "cancelled"
        release.set()
    finally:
        release.set()
        registry.close()


def test_restart_marks_running_job_unknown_and_never_reruns(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    store = JobStore()
    store.save({
        "job_id": "restored-job",
        "state": "running",
        "process_control": "managed",
        "cancellation_support": "local_process_tree",
        "created_at": "2026-11-06T00:00:00+00:00",
        "updated_at": "2026-11-06T00:00:01+00:00",
        "workdir": "C:\\work",
        "pid": 999999,
        "remote_termination": "unknown",
    })
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.run_agy",
        lambda *args, **kwargs: pytest.fail("restored job must not rerun"),
    )
    registry = AgyJobRegistry(store=store)
    try:
        status = registry.status("restored-job")
    finally:
        registry.close()

    assert status["state"] == "unknown"
    assert status["process_control"] == "managed"
    assert status["restart_recovered"] is True
    assert status["remote_termination"] == "unknown"


def test_managed_job_cancel_reports_confirmed_local_tree(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    invocation = ManagedAgyInvocation(
        command=(sys.executable, "-c", "print('ok')"),
        cwd=str(tmp_path),
        env=dict(os.environ),
        capability_id=None,
        workdir=str(tmp_path),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.prepare_managed_agy",
        lambda *args, **kwargs: invocation,
    )

    class FakeManaged:
        timeout = 30.0

        def __init__(self, on_started):
            self._on_started = on_started
            self.started = Event()
            self.cancel = Event()
            self.exit_observed = False

        def run(self):
            self._on_started(4242)
            self.started.set()
            self.cancel.wait(timeout=5)
            self.exit_observed = True
            return ManagedProcessOutcome(
                stdout="",
                stderr="",
                exit_code=1,
                completed=False,
                timed_out=False,
                cancelled=True,
            )

        def request_cancel(self):
            self.cancel.set()

        def termination_evidence(self):
            return {
                "termination_requested": self.cancel.is_set(),
                "termination_confirmed": self.cancel.is_set() and self.exit_observed,
                "local_termination": (
                    "confirmed" if self.exit_observed else "unknown"
                ),
                "remote_termination": "unknown",
                "process_exit_observed": self.exit_observed,
                "tree_kill_attempted": self.cancel.is_set(),
                "tree_kill_succeeded": self.exit_observed,
                "job_object_assigned": True,
                "pid": 4242,
            }

    holder = {}

    def factory(invocation, on_started):
        holder["process"] = FakeManaged(on_started)
        return holder["process"]

    registry = AgyJobRegistry(store=JobStore(), managed_factory=factory)
    try:
        job_id = registry.start("Work", process_control="managed")
        assert holder["process"].started.wait(timeout=2)
        status = registry.status(job_id)
        assert status["process_control"] == "managed"
        assert status["cancellation_support"] == "local_process_tree"

        report = registry.cancel(job_id, wait_timeout=2)

        assert report["state"] == "cancelled"
        assert report["cancel_requested"] is True
        assert report["termination_confirmed"] is True
        assert report["remote_termination"] == "unknown"
        assert registry.status(job_id)["state"] == "cancelled"
    finally:
        registry.close()


def test_managed_queued_cancel_prevents_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.prepare_managed_agy",
        lambda *args, **kwargs: ManagedAgyInvocation(
            command=(sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=str(tmp_path),
            env=dict(os.environ),
            capability_id=None,
            workdir=str(tmp_path),
        ),
    )
    first_started = Event()
    release = Event()
    calls = {"count": 0}

    def blocking_legacy(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            first_started.set()
            release.wait(timeout=5)
        return AgyResult(text="DONE", exit_code=0)

    monkeypatch.setattr("codex_agy_bridge.agy_jobs.run_agy", blocking_legacy)
    started_processes = []

    def factory(invocation, on_started):
        process = __import__(
            "codex_agy_bridge.agy_process",
            fromlist=["ManagedAgyProcess"],
        ).ManagedAgyProcess(
            command=invocation.command,
            cwd=invocation.cwd,
            env=invocation.env,
            timeout=30,
            on_started=on_started,
        )
        started_processes.append(process)
        return process

    registry = AgyJobRegistry(max_workers=1, managed_factory=factory)
    try:
        first = registry.start("First")
        assert first_started.wait(timeout=2)
        queued = registry.start("Queued", process_control="managed")

        report = registry.cancel(queued)

        assert report["queued_cancelled"] is True
        assert report["local_termination"] == "not_started"
        assert registry.status(queued)["state"] == "cancelled"
        assert started_processes[0].pid is None
        release.set()
    finally:
        release.set()
        registry.close()


def test_managed_cancel_does_not_kill_unrelated_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "codex_agy_bridge.agy_jobs.prepare_managed_agy",
        lambda *args, **kwargs: ManagedAgyInvocation(
            command=(sys.executable, "-c", "print('fixture')"),
            cwd=str(tmp_path),
            env=dict(os.environ),
            capability_id=None,
            workdir=str(tmp_path),
        ),
    )
    created = []

    class FakeManaged:
        timeout = 10.0

        def __init__(self, index, on_started):
            self.index = index
            self.on_started = on_started
            self.started = Event()
            self.cancel = Event()
            self.exit_observed = False

        def run(self):
            self.on_started(5000 + self.index)
            self.started.set()
            if self.index == 1:
                self.cancel.wait(timeout=3)
                self.exit_observed = True
                return ManagedProcessOutcome(
                    stdout="",
                    stderr="",
                    exit_code=1,
                    completed=False,
                    timed_out=False,
                    cancelled=True,
                )
            time.sleep(0.1)
            self.exit_observed = True
            payload = json.dumps({
                "conversation_id": "other-job",
                "status": "SUCCESS",
                "response": "OTHER_DONE",
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
            self.cancel.set()

        def termination_evidence(self):
            return {
                "termination_requested": self.cancel.is_set(),
                "termination_confirmed": self.cancel.is_set() and self.exit_observed,
                "local_termination": (
                    "confirmed" if self.cancel.is_set() else "not_requested"
                ),
                "remote_termination": "unknown",
                "process_exit_observed": self.exit_observed,
                "tree_kill_attempted": self.cancel.is_set(),
                "tree_kill_succeeded": self.exit_observed,
                "job_object_assigned": True,
                "pid": 5000 + self.index,
            }

    def factory(invocation, on_started):
        process = FakeManaged(len(created) + 1, on_started)
        created.append(process)
        return process

    registry = AgyJobRegistry(managed_factory=factory)
    try:
        first = registry.start("First", process_control="managed")
        other = registry.start("Other", process_control="managed")
        assert created[0].started.wait(timeout=2)
        assert created[1].started.wait(timeout=2)

        registry.cancel(first, wait_timeout=2)
        deadline = time.monotonic() + 2
        while registry.status(other)["state"] in {"queued", "running"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        status = registry.status(other)

        assert status["state"] == "completed"
        assert status["text"]
        assert created[1].cancel.is_set() is False
        completed_cancel = registry.cancel(other)
        assert completed_cancel["cancel_requested"] is False
        assert completed_cancel["local_termination"] == "already_finished"
        assert created[1].cancel.is_set() is False
    finally:
        registry.close()
