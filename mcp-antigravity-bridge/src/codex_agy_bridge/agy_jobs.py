"""Asynchronous AGY jobs with legacy and explicitly managed lifecycles."""

from __future__ import annotations

import atexit
import math
from concurrent.futures import (
    CancelledError,
    Future,
    ThreadPoolExecutor,
)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Any, Callable, Mapping
from uuid import uuid4

from .agy_job_store import JobStore
from .agy_policy import ExecutionOptions
from .agy_process import ManagedAgyProcess, ManagedProcessOutcome
from .agy_result import (
    AgyResult,
    agy_result_error_kind,
    describe_agy_failure,
    failure_details,
    parse_agy_result,
)
from .agy_runner import (
    ManagedAgyInvocation,
    prepare_managed_agy,
    run_agy,
    run_agy_visible,
)

_PROCESS_CONTROLS = {"legacy", "managed"}


@dataclass
class _JobRecord:
    future: Future[AgyResult] | None
    process_control: str
    created_at: str
    state: str = "queued"
    completed_at: float | None = None
    cancel_requested: bool = False
    process: ManagedAgyProcess | None = None
    pid: int | None = None
    timeout: float = 300.0
    workdir: str | None = None
    result: AgyResult | None = None
    extra: dict[str, Any] = field(default_factory=dict)


ManagedFactory = Callable[
    [ManagedAgyInvocation, Callable[[int], None]], ManagedAgyProcess
]


class AgyJobRegistry:
    """Run bounded agy calls and retain cancellable, restart-safe evidence."""

    def __init__(
        self,
        max_workers: int = 4,
        retention_seconds: float = 900.0,
        *,
        store: JobStore | None = None,
        managed_factory: ManagedFactory | None = None,
    ) -> None:
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be non-negative")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="codex-agy",
        )
        self._retention_seconds = retention_seconds
        self._jobs: dict[str, _JobRecord] = {}
        self._lock = RLock()
        self._closed = False
        self._store = store or JobStore()
        self._managed_factory = managed_factory

    def _prune_locked(self) -> int:
        cutoff = monotonic() - self._retention_seconds
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if record.completed_at is not None and record.completed_at <= cutoff
        ]
        for job_id in expired:
            del self._jobs[job_id]
        return len(expired)

    def _state_record(self, job_id: str, record: _JobRecord) -> dict[str, Any]:
        status: dict[str, Any] = {
            "job_id": job_id,
            "state": record.state,
            "process_control": record.process_control,
            "cancellation_support": (
                "local_process_tree"
                if record.process_control == "managed"
                else "queued_only"
            ),
            "created_at": record.created_at,
            "timeout": record.timeout,
            "workdir": record.workdir,
            "pid": record.pid,
            "remote_termination": "unknown",
        }
        if record.process is not None:
            status.update(record.process.termination_evidence())
        status.update(record.extra)
        return status

    def _persist_locked(self, job_id: str, record: _JobRecord) -> None:
        try:
            self._store.save(self._state_record(job_id, record))
        except (OSError, ValueError):
            pass

    def _mark_running(self, job_id: str, pid: int) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.state = "running"
                record.pid = pid
                self._persist_locked(job_id, record)

    def _managed_worker(
        self,
        job_id: str,
        invocation: ManagedAgyInvocation,
        process: ManagedAgyProcess,
    ) -> AgyResult:
        outcome: ManagedProcessOutcome = process.run()
        if outcome.cancelled:
            result = AgyResult(
                text="managed agy process was cancelled",
                exit_code=outcome.exit_code if outcome.exit_code is not None else -1,
                stderr=outcome.stderr,
                reason="cancelled",
                retryable=False,
                evidence="unavailable",
                workdir=invocation.workdir,
                capability_id=invocation.capability_id,
                output_format="stream-json",
            )
        elif outcome.timed_out:
            result = AgyResult(
                text=f"managed agy process timed out after {process.timeout:g}s",
                exit_code=outcome.exit_code if outcome.exit_code is not None else -1,
                stderr=outcome.stderr,
                reason="timeout",
                retryable=False,
                evidence="unavailable",
                workdir=invocation.workdir,
                capability_id=invocation.capability_id,
                output_format="stream-json",
            )
        else:
            result = parse_agy_result(
                outcome.stdout,
                outcome.stderr,
                outcome.exit_code or 0,
                output_format="stream-json",
                workdir=invocation.workdir,
                capability_id=invocation.capability_id,
            )
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.result = result
        return result

    def start(
        self,
        prompt: str,
        workdir: str | None = None,
        timeout: float = 300.0,
        output_format: str | None = None,
        dangerously_skip_permissions: bool = False,
        display_mode: str = "headless",
        execution_options: ExecutionOptions | None = None,
        process_control: str = "legacy",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        if process_control not in _PROCESS_CONTROLS:
            raise ValueError("process_control must be 'legacy' or 'managed'")
        if display_mode not in {"headless", "terminal"}:
            raise ValueError("display_mode must be 'headless' or 'terminal'")
        if process_control == "managed" and display_mode == "terminal":
            raise ValueError("managed process_control only supports headless mode")
        job_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        invocation: ManagedAgyInvocation | None = None
        if process_control == "managed":
            invocation = prepare_managed_agy(
                prompt,
                workdir=workdir,
                dangerously_skip_permissions=dangerously_skip_permissions,
                execution_options=execution_options,
            )
        with self._lock:
            if self._closed:
                raise RuntimeError("agy job registry is closed")
            self._prune_locked()
            record = _JobRecord(
                future=None,
                process_control=process_control,
                created_at=created_at,
                timeout=timeout,
                workdir=workdir,
            )
            if metadata:
                record.extra.update(dict(metadata))
            self._jobs[job_id] = record
            self._persist_locked(job_id, record)

        if process_control == "managed":
            assert invocation is not None
            on_started = lambda pid: self._mark_running(job_id, pid)
            if self._managed_factory is None:
                process = ManagedAgyProcess(
                    command=invocation.command,
                    cwd=invocation.cwd,
                    env=invocation.env,
                    timeout=timeout,
                    on_started=on_started,
                )
            else:
                process = self._managed_factory(invocation, on_started)
            with self._lock:
                self._jobs[job_id].process = process
            future = self._executor.submit(
                self._managed_worker,
                job_id,
                invocation,
                process,
            )
        else:
            runner = run_agy_visible if display_mode == "terminal" else run_agy
            future = self._executor.submit(
                runner,
                prompt,
                workdir=workdir,
                timeout=timeout,
                output_format=output_format,
                dangerously_skip_permissions=dangerously_skip_permissions,
                execution_options=execution_options,
            )
        with self._lock:
            record = self._jobs[job_id]
            record.future = future
            future.add_done_callback(lambda _future: self._mark_completed(job_id))
            self._persist_locked(job_id, record)
        return job_id

    def _mark_completed(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.completed_at = monotonic()
                if record.future is not None and record.future.cancelled():
                    record.state = "cancelled"
                self._persist_locked(job_id, record)

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            record = self._jobs.get(job_id)
            if record is None:
                stored = self._store.load(job_id)
                if stored is None:
                    return {
                        "job_id": job_id,
                        "state": "unknown",
                        "process_control": "unknown",
                        "cancellation_support": "none",
                        "error": "job not found",
                        "remote_termination": "unknown",
                    }
                recovered = dict(stored)
                recovered["restart_recovered"] = True
                recovered["cancellation_support"] = "none"
                if recovered.get("state") in {"queued", "running"}:
                    recovered["state"] = "unknown"
                    recovered["error"] = "job state cannot be proven after bridge restart"
                return recovered
            future = record.future

        if future is None:
            return self._state_record(job_id, record)
        if future.cancelled():
            with self._lock:
                record.state = "cancelled"
                self._persist_locked(job_id, record)
            status = self._state_record(job_id, record)
            status["error"] = "job was cancelled before execution"
            return status
        if not future.done():
            with self._lock:
                record.state = "running" if future.running() else "queued"
                self._persist_locked(job_id, record)
            return self._state_record(job_id, record)

        try:
            result = future.result()
        except CancelledError:
            with self._lock:
                record.state = "cancelled"
                self._persist_locked(job_id, record)
            return self._state_record(job_id, record)
        except Exception as exc:  # noqa: BLE001 - expose worker failure.
            with self._lock:
                record.state = "failed"
                record.extra["error"] = str(exc)
                self._persist_locked(job_id, record)
            return self._state_record(job_id, record)

        error_kind = agy_result_error_kind(result)
        if record.cancel_requested and result.reason == "cancelled":
            state = "cancelled"
        else:
            state = "completed" if error_kind is None else "failed"
        with self._lock:
            record.state = state
            record.result = result
            self._persist_locked(job_id, record)
        status = self._state_record(job_id, record)
        status.update({
            "text": result.text,
            "exit_code": result.exit_code,
            "used_pty": result.used_pty,
        })
        if error_kind is not None:
            for key, value in failure_details(result).items():
                if value is not None:
                    status[key] = value
            status["error"] = describe_agy_failure(result)
        return status

    def cancel(self, job_id: str, wait_timeout: float = 10.0) -> dict[str, Any]:
        if (
            isinstance(wait_timeout, bool)
            or not isinstance(wait_timeout, (int, float))
            or not math.isfinite(float(wait_timeout))
            or wait_timeout < 0
        ):
            raise ValueError("wait_timeout must be a non-negative finite number")
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                stored = self._store.load(job_id)
                return {
                    "job_id": job_id,
                    "state": "unknown",
                    "cancel_requested": False,
                    "cancellation_supported": False,
                    "termination_confirmed": False,
                    "local_termination": "unknown",
                    "remote_termination": "unknown",
                    "restart_recovered": stored is not None,
                    "error": "job not found in current bridge process",
                }
            future = record.future
            process = record.process
            process_control = record.process_control

        if process_control == "legacy":
            if future is not None and future.cancel():
                with self._lock:
                    record.state = "cancelled"
                    record.cancel_requested = True
                    self._persist_locked(job_id, record)
                return {
                    "job_id": job_id,
                    "state": "cancelled",
                    "cancel_requested": True,
                    "queued_cancelled": True,
                    "cancellation_supported": True,
                    "termination_confirmed": True,
                    "local_termination": "not_started",
                    "remote_termination": "unknown",
                    "wait_timed_out": False,
                }
            if future is not None and future.running():
                with self._lock:
                    record.cancel_requested = True
                    self._persist_locked(job_id, record)
                return {
                    "job_id": job_id,
                    "state": "unsupported_running_legacy",
                    "cancel_requested": False,
                    "cancellation_supported": False,
                    "termination_confirmed": False,
                    "local_termination": "unknown",
                    "remote_termination": "unknown",
                    "wait_timed_out": False,
                    "error": "running legacy jobs cannot be safely cancelled",
                }
            current = self.status(job_id)
            return {
                "job_id": job_id,
                "state": current["state"],
                "cancel_requested": False,
                "cancellation_supported": False,
                "termination_confirmed": False,
                "local_termination": "already_finished",
                "remote_termination": "unknown",
                "wait_timed_out": False,
            }

        if process is None:
            raise RuntimeError("managed job has no process handle")
        process.request_cancel()
        with self._lock:
            record.cancel_requested = True
        queued_cancelled = bool(future is not None and future.cancel())
        if queued_cancelled:
            with self._lock:
                record.state = "cancelled"
                self._persist_locked(job_id, record)
        elif future is not None and not future.done() and wait_timeout > 0:
            try:
                future.result(timeout=wait_timeout)
            except TimeoutError:
                pass
        evidence = process.termination_evidence()
        current = self.status(job_id)
        return {
            "job_id": job_id,
            "state": current["state"],
            "cancel_requested": True,
            "queued_cancelled": queued_cancelled,
            "cancellation_supported": True,
            **evidence,
            "wait_timed_out": bool(future is not None and not future.done()),
        }

    def cleanup(self) -> int:
        with self._lock:
            return self._prune_locked()

    def close(self, wait: bool = True, cancel_futures: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            processes = [
                record.process
                for record in self._jobs.values()
                if record.process is not None
                and not record.process.termination_evidence()["process_exit_observed"]
            ]
        for process in processes:
            process.request_cancel()
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def __enter__(self) -> "AgyJobRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


agy_jobs = AgyJobRegistry()
atexit.register(agy_jobs.close)
