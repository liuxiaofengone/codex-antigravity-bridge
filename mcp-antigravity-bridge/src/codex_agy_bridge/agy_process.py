"""Managed local process execution with explicit cancellation evidence."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ManagedProcessOutcome:
    stdout: str
    stderr: str
    exit_code: int | None
    completed: bool
    timed_out: bool
    cancelled: bool


class ManagedAgyProcess:
    """One local process tree owned by one job."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout: float,
        *,
        on_started: Callable[[int], None] | None = None,
    ) -> None:
        if not command or not all(isinstance(item, str) for item in command):
            raise ValueError("managed command must be a non-empty string sequence")
        if timeout <= 0:
            raise ValueError("managed timeout must be positive")
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.timeout = float(timeout)
        self._on_started = on_started
        self._cancel_event = threading.Event()
        self._cancel_requested_at: float | None = None
        self._process: subprocess.Popen[str] | None = None
        self._pid: int | None = None
        self._started = False
        self._termination = threading.Event()
        self._process_exit_observed = False
        self._tree_kill_attempted = False
        self._tree_kill_succeeded = False
        self._windows_job: object | None = None
        self._job_object_assigned = False
        self._lock = threading.RLock()

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._pid

    def request_cancel(self) -> None:
        with self._lock:
            if self._cancel_requested_at is None:
                self._cancel_requested_at = time.time()
            self._cancel_event.set()
            if not self._started:
                self._termination.set()

    def run(self) -> ManagedProcessOutcome:
        with self._lock:
            if self._started:
                raise RuntimeError("managed process can only be run once")
            self._started = True
        if self._cancel_event.is_set():
            return ManagedProcessOutcome(
                stdout="",
                stderr="",
                exit_code=None,
                completed=False,
                timed_out=False,
                cancelled=True,
            )

        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = 0x08000000 | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP
        process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        with self._lock:
            self._process = process
            self._pid = process.pid
        if self._on_started is not None:
            self._on_started(process.pid)

        if os.name == "nt":
            self._windows_job = _WindowsJobObject.create(process.pid)
            self._job_object_assigned = self._windows_job is not None

        captured: dict[str, str] = {"stdout": "", "stderr": ""}
        readers = [
            threading.Thread(
                target=self._read_stream,
                args=(process.stdout, "stdout", captured),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=(process.stderr, "stderr", captured),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        timeout_occurred = False
        cancelled = False
        deadline = time.monotonic() + self.timeout
        while process.poll() is None:
            if self._cancel_event.is_set():
                cancelled = True
                self._terminate_process_tree()
                break
            if time.monotonic() >= deadline:
                timeout_occurred = True
                self._terminate_process_tree()
                break
            time.sleep(0.03)

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._process_exit_observed = process.poll() is not None
        for reader in readers:
            reader.join(timeout=2)
        if self._windows_job is not None:
            self._windows_job.close()
        return ManagedProcessOutcome(
            stdout=captured["stdout"],
            stderr=captured["stderr"],
            exit_code=process.returncode,
            completed=not timeout_occurred and not cancelled and process.returncode == 0,
            timed_out=timeout_occurred,
            cancelled=cancelled,
        )

    def termination_evidence(self) -> dict[str, object]:
        with self._lock:
            requested = self._cancel_requested_at is not None
            confirmed = self._tree_kill_succeeded and self._process_exit_observed
            if requested and not self._started:
                local_termination = "not_started"
                confirmed = True
            elif confirmed:
                local_termination = "confirmed"
            elif requested:
                local_termination = "unknown"
            else:
                local_termination = "not_requested"
            return {
                "termination_requested": requested,
                "termination_confirmed": confirmed,
                "local_termination": local_termination,
                "remote_termination": "unknown",
                "process_exit_observed": self._process_exit_observed,
                "tree_kill_attempted": self._tree_kill_attempted,
                "tree_kill_succeeded": self._tree_kill_succeeded,
                "job_object_assigned": self._job_object_assigned,
                "pid": self._pid,
            }

    def _terminate_process_tree(self) -> bool:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._tree_kill_attempted = True
            try:
                if os.name == "nt" and self._windows_job is not None:
                    ok = self._windows_job.terminate()
                elif os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                    ok = True
                else:
                    ok = _terminate_windows_process_tree(process.pid)
            except OSError:
                ok = False
            self._tree_kill_succeeded = bool(ok)
            return bool(ok)

    @staticmethod
    def _read_stream(
        stream: object,
        key: str,
        captured: dict[str, str],
    ) -> None:
        if stream is None:
            return
        try:
            captured[key] = stream.read()
        except (OSError, ValueError):
            captured[key] = ""


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _WindowsJobObject:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

        def __init__(self, handle: int) -> None:
            self._handle = handle

        @classmethod
        def create(cls, pid: int) -> "_WindowsJobObject | None":
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            job = cls(handle)
            info = _JobExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = (
                cls.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            configured = kernel32.SetInformationJobObject(
                handle,
                cls.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not configured:
                job.close()
                return None
            process_handle = kernel32.OpenProcess(
                0x0001 | 0x0200 | 0x0400 | 0x1000,
                False,
                pid,
            )
            if not process_handle:
                job.close()
                return None
            try:
                if not kernel32.AssignProcessToJobObject(handle, process_handle):
                    job.close()
                    return None
            finally:
                kernel32.CloseHandle(process_handle)
            return job

        def terminate(self) -> bool:
            if not self._handle:
                return False
            return bool(ctypes.windll.kernel32.TerminateJobObject(self._handle, 1))

        def close(self) -> None:
            if self._handle:
                ctypes.windll.kernel32.CloseHandle(self._handle)
                self._handle = 0

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    def _windows_descendants(root_pid: int) -> list[int]:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or snapshot == invalid_handle:
            return []
        parents: dict[int, int] = {}
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
        descendants: list[int] = []
        frontier = [root_pid]
        while frontier:
            parent = frontier.pop()
            children = [
                pid for pid, parent_pid in parents.items() if parent_pid == parent
            ]
            descendants.extend(children)
            frontier.extend(children)
        return list(dict.fromkeys(descendants))

    def _terminate_windows_process_tree(root_pid: int) -> bool:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        pids = list(reversed(_windows_descendants(root_pid))) + [root_pid]
        handles: list[int] = []
        all_opened = True
        for pid in pids:
            handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
            if handle:
                handles.append(handle)
            elif pid != root_pid:
                all_opened = False
        for handle in handles:
            kernel32.TerminateProcess(handle, 1)
        for handle in handles:
            kernel32.WaitForSingleObject(handle, 3000)
            kernel32.CloseHandle(handle)
        remaining = _windows_descendants(root_pid)
        return all_opened and not remaining
