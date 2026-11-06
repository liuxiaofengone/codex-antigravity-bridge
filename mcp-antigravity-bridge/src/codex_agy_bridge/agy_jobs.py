"""Asynchronous job management for parallel worktree delegation."""

# Modified by liuxiaofengone, 2026-10-10: propagate permission and empty-response failures.

from __future__ import annotations

import atexit
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Any
from uuid import uuid4

from .agy_policy import ExecutionOptions
from .agy_runner import (
    AgyResult,
    agy_result_error_kind,
    describe_agy_failure,
    failure_details,
    run_agy,
    run_agy_visible,
)


@dataclass
class _JobRecord:
    future: Future[AgyResult]
    completed_at: float | None = None


class AgyJobRegistry:
    """Run bounded agy calls and retain completed results for a finite period."""

    def __init__(self, max_workers: int = 4, retention_seconds: float = 900.0) -> None:
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

    def _mark_completed(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.completed_at = monotonic()

    def start(
        self,
        prompt: str,
        workdir: str | None = None,
        timeout: float = 300.0,
        output_format: str | None = None,
        dangerously_skip_permissions: bool = False,
        display_mode: str = "headless",
        execution_options: ExecutionOptions | None = None,
    ) -> str:
        if display_mode not in {"headless", "terminal"}:
            raise ValueError("display_mode must be 'headless' or 'terminal'")
        job_id = uuid4().hex
        with self._lock:
            if self._closed:
                raise RuntimeError("agy job registry is closed")
            self._prune_locked()
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
            self._jobs[job_id] = _JobRecord(future=future)
            future.add_done_callback(lambda _future: self._mark_completed(job_id))
        return job_id

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            record = self._jobs.get(job_id)
            if record is None:
                return {"job_id": job_id, "state": "unknown", "error": "job not found"}
            future = record.future

        if not future.done():
            return {
                "job_id": job_id,
                "state": "running" if future.running() else "queued",
            }

        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 - report worker failures to MCP.
            return {"job_id": job_id, "state": "failed", "error": str(exc)}

        error_kind = agy_result_error_kind(result)
        state = "completed" if error_kind is None else "failed"
        status = {
            "job_id": job_id,
            "state": state,
            "text": result.text,
            "exit_code": result.exit_code,
            "used_pty": result.used_pty,
        }
        if state == "failed":
            for key, value in failure_details(result).items():
                if value is not None:
                    status[key] = value
            status["error"] = describe_agy_failure(result)
        return status

    def cleanup(self) -> int:
        """Remove completed jobs older than the configured retention period."""
        with self._lock:
            return self._prune_locked()

    def close(self, wait: bool = True, cancel_futures: bool = True) -> None:
        """Stop accepting jobs and release the worker pool."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def __enter__(self) -> "AgyJobRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


agy_jobs = AgyJobRegistry()
atexit.register(agy_jobs.close)
