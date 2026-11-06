"""Real local managed-process lifecycle tests; no AGY model call is made."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from codex_agy_bridge.agy_process import ManagedAgyProcess


def _powershell() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell"


def _run_python_script(script: Path, timeout: float = 15) -> ManagedAgyProcess:
    return ManagedAgyProcess(
        command=[sys.executable, str(script)],
        cwd=str(script.parent),
        env=dict(os.environ),
        timeout=timeout,
    )


def _windows_pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
            process, ctypes.byref(exit_code)
        ):
            return False
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def test_managed_process_captures_stdout_and_stderr(tmp_path: Path) -> None:
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\n"
        "print('STDOUT_OK', flush=True)\n"
        "print('STDERR_OK', file=sys.stderr, flush=True)\n",
        encoding="utf-8",
    )

    outcome = _run_python_script(script).run()

    assert outcome.completed is True
    assert outcome.timed_out is False
    assert outcome.cancelled is False
    assert outcome.exit_code == 0
    assert "STDOUT_OK" in outcome.stdout
    assert "STDERR_OK" in outcome.stderr


def test_managed_process_timeout_terminates_local_process(tmp_path: Path) -> None:
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    process = _run_python_script(script, timeout=0.2)

    started = time.monotonic()
    outcome = process.run()

    assert outcome.timed_out is True
    assert time.monotonic() - started < 10
    evidence = process.termination_evidence()
    assert evidence["remote_termination"] == "unknown"
    assert evidence["process_exit_observed"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_windows_cancel_terminates_powershell_child_tree(tmp_path: Path) -> None:
    child_script = tmp_path / "child.py"
    child_pid_file = tmp_path / "child.pid"
    child_script.write_text(
        "import os,time\n"
        f"open({str(child_pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent_script = tmp_path / "parent.ps1"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    parent_script.write_text(
        f"& {quote(sys.executable)} {quote(str(child_script))}\n",
        encoding="utf-8",
    )
    process = ManagedAgyProcess(
        command=[
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(parent_script),
        ],
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout=30,
    )
    thread = threading.Thread(target=process.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        text = (
            child_pid_file.read_text(encoding="utf-8").strip()
            if child_pid_file.exists()
            else ""
        )
        if text:
            child_pid = int(text)
            break
        time.sleep(0.05)
    else:
        pytest.fail("PowerShell child PID was not created")

    assert _windows_pid_alive(child_pid)
    process.request_cancel()
    thread.join(timeout=15)
    assert not thread.is_alive()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _windows_pid_alive(child_pid):
        time.sleep(0.05)
    evidence = process.termination_evidence()
    assert _windows_pid_alive(child_pid) is False, evidence
    assert evidence["tree_kill_succeeded"] is True
    assert evidence["termination_confirmed"] is True


def test_empty_output_is_not_retried(tmp_path: Path) -> None:
    counter = tmp_path / "runs.txt"
    script = tmp_path / "empty.py"
    script.write_text(
        "from pathlib import Path\n"
        f"p=Path({str(counter)!r})\n"
        "p.write_text((p.read_text() if p.exists() else '') + 'x')\n",
        encoding="utf-8",
    )

    outcome = _run_python_script(script).run()

    assert outcome.stdout == ""
    assert outcome.stderr == ""
    assert counter.read_text(encoding="utf-8") == "x"


def test_termination_failure_is_not_reported_confirmed(
    tmp_path: Path, monkeypatch
) -> None:
    script = tmp_path / "short.py"
    script.write_text("import time\ntime.sleep(0.5)\n", encoding="utf-8")
    process = _run_python_script(script)
    monkeypatch.setattr(process, "_terminate_process_tree", lambda: False)
    thread = threading.Thread(target=process.run, daemon=True)
    thread.start()
    process.request_cancel()
    thread.join(timeout=5)

    evidence = process.termination_evidence()
    assert evidence["termination_requested"] is True
    assert evidence["termination_confirmed"] is False
    assert evidence["local_termination"] != "confirmed"
