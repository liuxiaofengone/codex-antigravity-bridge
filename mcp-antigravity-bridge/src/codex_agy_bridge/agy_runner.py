"""Headless runner for the Google Antigravity CLI (`agy`).

`agy -p "<prompt>"` gates its stdout on `isatty()` in some builds (upstream
issue #76): when stdout is not attached to a real terminal it can emit
nothing and exit 0. This module tries the plain subprocess path first and,
when the result comes back empty, re-runs `agy` inside a freshly allocated
pseudo-terminal (ConPTY on Windows via `pywinpty`, `pty` on POSIX), then
strips ANSI / TUI chrome from the captured output.

Reference implementations:
- https://github.com/rhishi99/agy-headless-bridge (pty/ConPTY fix)
- https://github.com/sshahzaiib/agy-bridge (MCP bridge, TypeScript)
"""

# Modified by liuxiaofengone, 2026-10-10: validate headless outcomes and decode UTF-8 output.

from __future__ import annotations

import os
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .agy_capabilities import inspect_agy_capabilities
from .agy_policy import (
    ExecutionOptions,
    build_session_args,
    load_inherited_permissions,
)
from .agy_result import (
    AgyResult,
    agy_result_error_kind,
    describe_agy_failure,
    failure_details,
    parse_agy_result,
)


@dataclass(frozen=True)
class ManagedAgyInvocation:
    command: tuple[str, ...]
    cwd: str | None
    env: dict[str, str]
    capability_id: str | None
    workdir: str | None

# --- ANSI / TUI noise stripping -------------------------------------------

_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_OTHER = re.compile(r"\x1b[@-Z\\-_]")
# Box-drawing / spinner glyphs agy uses for its TUI chrome.
_SPINNER = set(
    "⠁⠂⠄⡀⢀⠠⠐⠈⣾⣽⣻⢿⡿⣟⣯⣷⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    "│─┌┐└┘├┤┬┴┼╭╮╰╯═║╔╗╚╝▌▐█▏▕"
)

_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_PROXY_ENV_NAMES = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
)
_PROXY_PORTS = (7890, 7891, 7892, 7897, 1080, 10808, 10809, 8080, 8888, 3128)
_PROXY_CACHE_TTL = 60.0
_proxy_cache: tuple[float, Optional[str]] | None = None
_proxy_cache_lock = threading.Lock()

_NETWORK_ERROR_MARKERS = (
    "dial tcp",
    "connectex",
    "connection refused",
    "connection reset",
    "connection timed out",
    "network is unreachable",
    "no such host",
    "proxyconnect",
    "proxy connection",
    "connection failed",
    "failed to connect",
    "could not connect",
    "failed to fetch",
    "network error",
    "timed out",
    "timeout",
    "eof while connecting",
)
_PERMISSION_ERROR_MARKERS = (
    "user denied permission",
    "permission check failed",
    "tool call was denied",
    "tool call is denied",
    "soft-denied",
    "auto-denied",
    "permission denied for",
)
_AUTH_ERROR_MARKERS = (
    "authentication required",
    "authentication failed",
    "login required",
    "login to continue",
    "invalid token",
    "token is invalid",
    "invalid_token",
    "invalid_grant",
    "authorization failed",
    "unauthenticated",
    "oauth token",
    "please run `agy`",
    "run agy to log in",
)


def clean_agy_output(raw: str) -> str:
    """Strip ANSI escapes, carriage-return repaints and TUI chrome."""
    text = _ANSI_OSC.sub("", raw)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OTHER.sub("", text)
    text = text.replace("\r\n", "\n")
    out = []
    for line in text.split("\n"):
        visible = line.split("\r")[-1]
        had_chrome = any(c in _SPINNER for c in visible)
        stripped = "".join(c for c in visible if c not in _SPINNER).strip()
        if had_chrome and not stripped:
            continue  # pure decoration line
        out.append(stripped if had_chrome else visible.rstrip())
    return "\n".join(out).strip()


# --- agy discovery ---------------------------------------------------------


def find_agy() -> Optional[str]:
    """Locate the `agy` binary. Honors $AGY_PATH, then PATH, then OS defaults."""
    explicit = os.environ.get("AGY_PATH")
    if explicit and os.path.exists(explicit):
        return explicit

    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found

    home = os.path.expanduser("~")
    if sys.platform == "win32":
        candidates = [
            os.path.join(home, "AppData", "Local", "agy", "bin", "agy.exe"),
            os.path.join(home, "AppData", "Roaming", "agy", "bin", "agy.exe"),
        ]
    else:
        candidates = [
            os.path.join(home, ".local", "bin", "agy"),
            "/opt/antigravity/bin/agy",
            "/usr/local/bin/agy",
        ]
    return next((c for c in candidates if os.path.exists(c)), None)


def _windows_short_path(path: str) -> str:
    """Return an ASCII 8.3 path when Windows provides one."""
    if sys.platform != "win32" or path.isascii():
        return path

    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    if not length or length >= len(buffer):
        return path
    return buffer.value


def _agy_launch_prefix(binary: str) -> list[str]:
    """Run PowerShell AGY shims explicitly; never route through cmd.exe."""
    if sys.platform == "win32" and Path(binary).suffix.lower() == ".ps1":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            binary,
        ]
    return [binary]


def _normalise_proxy_url(value: str | None) -> Optional[str]:
    if not value or not value.strip():
        return None
    candidate = value.strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in _PROXY_SCHEMES or not parsed.hostname:
            return None
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            return None
    except ValueError:
        return None
    return candidate


def _environment_value(environ: dict[str, str], name: str) -> Optional[str]:
    for key in (name, name.lower()):
        value = environ.get(key)
        if value:
            return value
    return None


def _first_proxy_value(environ: dict[str, str], names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        proxy = _normalise_proxy_url(_environment_value(environ, name))
        if proxy:
            return proxy
    return None


def _is_local_proxy(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _probe_local_proxy_port(port: int, timeout: float = 0.25) -> Optional[str]:
    """Detect a local HTTP CONNECT or SOCKS5 listener without reaching the internet."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            request = (
                b"CONNECT oauth2.googleapis.com:443 HTTP/1.1\r\n"
                b"Host: oauth2.googleapis.com:443\r\n\r\n"
            )
            connection.sendall(request)
            response = connection.recv(128)
            if re.match(rb"HTTP/\d(?:\.\d)?\s+200\b", response):
                return f"http://127.0.0.1:{port}"
    except (OSError, ValueError):
        pass

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(b"\x05\x01\x00")
            response = connection.recv(2)
            if response == b"\x05\x00":
                return f"socks5://127.0.0.1:{port}"
    except (OSError, ValueError):
        pass
    return None


def _windows_system_proxy() -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except (ImportError, OSError):
        return None
    if enabled != 1 or not server:
        return None

    match = re.search(r"(?i)(?:https|http|all)=(?P<proxy>[^;]+)", str(server))
    value = match.group("proxy") if match else str(server)
    if "://" not in value:
        value = f"http://{value}"
    return _normalise_proxy_url(value)


def _discover_runtime_proxy() -> Optional[str]:
    system_proxy = _windows_system_proxy()
    if system_proxy:
        return system_proxy
    for port in _PROXY_PORTS:
        proxy = _probe_local_proxy_port(port)
        if proxy:
            return proxy
    return None


def _cached_runtime_proxy(force: bool = False) -> Optional[str]:
    global _proxy_cache
    now = time.monotonic()
    with _proxy_cache_lock:
        if _proxy_cache and not force and now - _proxy_cache[0] < _PROXY_CACHE_TTL:
            return _proxy_cache[1]
        proxy = _discover_runtime_proxy()
        _proxy_cache = (now, proxy)
        return proxy


def resolve_agy_environment(force: bool = False) -> dict[str, str]:
    """Return the AGY environment with a current usable proxy when one is found."""
    environment = dict(os.environ)
    explicit = _first_proxy_value(environment, ("AGY_PROXY_URL", "PROXY_URL"))
    inherited = _first_proxy_value(environment, _PROXY_ENV_NAMES)

    proxy = explicit or inherited
    if proxy and _is_local_proxy(proxy):
        parsed = urlsplit(proxy)
        detected = _probe_local_proxy_port(parsed.port or 0)
        proxy = detected or proxy
    elif not proxy:
        proxy = _cached_runtime_proxy(force=force)

    if proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            environment[name] = proxy
            environment[name.lower()] = proxy
    return environment


def classify_agy_error(text: str, stderr: str = "") -> str:
    """Classify an AGY failure for user-facing recovery guidance."""
    detail = f"{text}\n{stderr}".lower()
    if any(marker in detail for marker in _PERMISSION_ERROR_MARKERS):
        return "permission"
    if any(marker in detail for marker in _NETWORK_ERROR_MARKERS):
        return "network"
    if any(marker in detail for marker in _AUTH_ERROR_MARKERS):
        return "authentication"
    return "unknown"


def run_agy(
    prompt: str,
    workdir: Optional[str] = None,
    timeout: float = 300.0,
    output_format: Optional[str] = None,
    dangerously_skip_permissions: bool = False,
    execution_options: ExecutionOptions | None = None,
) -> AgyResult:
    """Run `agy -p <prompt>` headlessly and return cleaned text output.

    Falls back to a pseudo-terminal when the direct call returns empty
    (the isatty gate from upstream issue #76).
    """
    binary = find_agy()
    if binary is None:
        raise FileNotFoundError(
            "agy binary not found. Install it (Windows: "
            "`irm https://antigravity.google/cli/install.ps1 | iex`) or set AGY_PATH."
        )

    options = execution_options or ExecutionOptions()
    if not isinstance(options, ExecutionOptions):
        raise TypeError("execution_options must be an ExecutionOptions instance")
    if not options.is_default and dangerously_skip_permissions:
        raise ValueError(
            "execution_options conflict with dangerously_skip_permissions"
        )
    capability_id: str | None = None
    if options.is_default:
        session_args: list[str] = []
    else:
        capabilities = inspect_agy_capabilities()
        capability_id = capabilities.capability_id
        session_args = build_session_args(
            options,
            capabilities,
            load_inherited_permissions(),
        )
    args = [*_agy_launch_prefix(binary), "-p", prompt, *session_args]
    if output_format:
        args += ["--output-format", output_format]
    if dangerously_skip_permissions:
        args += ["--dangerously-skip-permissions"]

    launch_workdir = workdir
    pty_workdir = workdir
    if sys.platform == "win32" and workdir and not workdir.isascii():
        ascii_workdir = _windows_short_path(workdir)
        if ascii_workdir.isascii():
            launch_workdir = ascii_workdir
            pty_workdir = ascii_workdir
        else:
            # Fall back to an inherited cwd if Windows has no short path.
            args += ["--add-dir", workdir]
            launch_workdir = None
            pty_workdir = None

    environment = resolve_agy_environment()
    direct = _run_subprocess(args, launch_workdir, timeout, environment)
    direct_result = parse_agy_result(
        direct.stdout,
        direct.stderr,
        direct.returncode,
        output_format=output_format,
        used_pty=False,
        workdir=launch_workdir,
        capability_id=capability_id,
    )
    direct_text = direct_result.text
    direct_stderr = direct_result.stderr
    direct_clean_text = clean_agy_output(direct.stdout)
    direct_error = agy_result_error_kind(direct_result)
    # A denied tool is not the isatty bug. Re-running it in a PTY would repeat
    # the denied operation and cannot supply the missing user authorization.
    if (
        direct_clean_text
        or direct.returncode != 0
        or direct_error not in {None, "empty_output"}
    ):
        if classify_agy_error(direct_text, direct_stderr) == "network":
            resolve_agy_environment(force=True)
        return direct_result

    try:
        pty_text, exit_code = _run_with_pty(args, pty_workdir, timeout, environment)
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the direct failure context.
        fallback_text = direct_stderr or f"agy produced no output; PTY fallback failed: {exc}"
        return AgyResult(
            text=fallback_text,
            exit_code=direct.returncode if direct.returncode != 0 else -1,
            used_pty=True,
            stderr=direct_stderr,
        )

    cleaned_pty_text = clean_agy_output(pty_text)
    if cleaned_pty_text:
        return parse_agy_result(
            pty_text,
            direct_stderr,
            direct.returncode if exit_code == -1 else exit_code,
            output_format=output_format,
            used_pty=True,
            workdir=pty_workdir,
            capability_id=capability_id,
        )

    fallback_exit_code = (
        direct.returncode
        if direct.returncode != 0
        else (exit_code if exit_code != 0 else -1)
    )
    fallback_result = parse_agy_result(
        "",
        direct_stderr,
        0,
        output_format=output_format,
        used_pty=True,
        workdir=pty_workdir,
        capability_id=capability_id,
    )
    fallback_result.exit_code = fallback_exit_code
    fallback_result.text = "agy produced no output; PTY fallback was unavailable"
    return fallback_result


def prepare_managed_agy(
    prompt: str,
    workdir: Optional[str] = None,
    dangerously_skip_permissions: bool = False,
    execution_options: ExecutionOptions | None = None,
) -> ManagedAgyInvocation:
    """Build one direct stream-json AGY process invocation for a managed job."""
    binary = find_agy()
    if binary is None:
        raise FileNotFoundError(
            "agy binary not found. Install it or set AGY_PATH before using managed jobs."
        )
    options = execution_options or ExecutionOptions()
    if not isinstance(options, ExecutionOptions):
        raise TypeError("execution_options must be an ExecutionOptions instance")
    if not options.is_default and dangerously_skip_permissions:
        raise ValueError(
            "execution_options conflict with dangerously_skip_permissions"
        )
    capability_id: str | None = None
    if options.is_default:
        session_args: list[str] = []
    else:
        capabilities = inspect_agy_capabilities()
        capability_id = capabilities.capability_id
        session_args = build_session_args(
            options,
            capabilities,
            load_inherited_permissions(),
        )
    launch_workdir = workdir
    command = [
        *_agy_launch_prefix(binary),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        *session_args,
    ]
    if dangerously_skip_permissions:
        command.append("--dangerously-skip-permissions")
    if sys.platform == "win32" and workdir and not workdir.isascii():
        ascii_workdir = _windows_short_path(workdir)
        if ascii_workdir.isascii():
            launch_workdir = ascii_workdir
        else:
            command.extend(["--add-dir", workdir])
            launch_workdir = None
    return ManagedAgyInvocation(
        command=tuple(command),
        cwd=launch_workdir,
        env=resolve_agy_environment(),
        capability_id=capability_id,
        workdir=workdir,
    )


def run_agy_visible(
    prompt: str,
    workdir: Optional[str] = None,
    timeout: float = 300.0,
    output_format: Optional[str] = None,
    dangerously_skip_permissions: bool = False,
    execution_options: ExecutionOptions | None = None,
) -> AgyResult:
    """Run agy in a visible Windows console and wait for its exit code.

    The console inherits agy's live terminal output. Output is intentionally
    not captured here because the user is watching the terminal; the job
    status still records the exit code and a short handoff message.
    """
    if sys.platform != "win32":
        raise RuntimeError("visible terminal mode is currently supported on Windows only")

    binary = find_agy()
    if binary is None:
        raise FileNotFoundError(
            "agy binary not found. Install it or set AGY_PATH before using terminal mode."
        )

    options = execution_options or ExecutionOptions()
    if not isinstance(options, ExecutionOptions):
        raise TypeError("execution_options must be an ExecutionOptions instance")
    if not options.is_default and dangerously_skip_permissions:
        raise ValueError(
            "execution_options conflict with dangerously_skip_permissions"
        )
    capability_id: str | None = None
    if options.is_default:
        session_args: list[str] = []
    else:
        capabilities = inspect_agy_capabilities()
        capability_id = capabilities.capability_id
        session_args = build_session_args(
            options,
            capabilities,
            load_inherited_permissions(),
        )
    args = [*_agy_launch_prefix(binary), "-p", prompt, *session_args]
    if output_format:
        args += ["--output-format", output_format]
    if dangerously_skip_permissions:
        args += ["--dangerously-skip-permissions"]

    launch_workdir = workdir
    if workdir and not workdir.isascii():
        launch_workdir = _windows_short_path(workdir)

    try:
        environment = resolve_agy_environment()
        process = subprocess.Popen(
            args,
            cwd=launch_workdir,
            creationflags=0x00000010,  # CREATE_NEW_CONSOLE
            env=environment,
        )
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise TimeoutError(f"agy timed out after {timeout}s") from exc

    return AgyResult(
        text="Live agy output was shown in a separate terminal window.",
        exit_code=exit_code,
        capability_id=capability_id,
    )


def _run_subprocess(
    args: list[str],
    workdir: Optional[str],
    timeout: float,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    # AGY uses UTF-8; the Windows locale may still be GBK. Explicit decoding
    # prevents reader-thread errors that otherwise replace the capture with None.
    kwargs: dict = {
        "cwd": workdir,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if env is not None:
        kwargs["env"] = env
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash
    try:
        return subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"agy timed out after {timeout}s") from exc


def _run_with_pty(
    args: list[str],
    workdir: Optional[str],
    timeout: float,
    env: Optional[dict[str, str]] = None,
) -> tuple[str, int]:
    """Run agy attached to a fresh pty so it believes stdout is a terminal."""
    if sys.platform == "win32":
        return _run_with_conpty(args, workdir, timeout, env)
    return _run_with_posix_pty(args, workdir, timeout, env)


def _run_with_conpty(
    args: list[str],
    workdir: Optional[str],
    timeout: float,
    env: Optional[dict[str, str]] = None,
) -> tuple[str, int]:
    try:
        from pywinpty import PtyProcess  # type: ignore
    except ImportError:
        return "", -1

    proc = PtyProcess.spawn(args, cwd=workdir, env=env)
    chunks: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"agy timed out after {timeout}s")
            try:
                chunk = proc.read(4096)
            except EOFError:
                break
            if not chunk:
                if proc.isalive():
                    time.sleep(0.1)
                    continue
                break
            chunks.append(chunk)
    finally:
        proc.terminate(force=True)
    return "".join(chunks), proc.exitstatus


def _run_with_posix_pty(
    args: list[str],
    workdir: Optional[str],
    timeout: float,
    env: Optional[dict[str, str]] = None,
) -> tuple[str, int]:
    import pty

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            args,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=workdir,
            close_fds=True,
            env=env,
        )
    finally:
        os.close(slave)

    chunks: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise TimeoutError(f"agy timed out after {timeout}s")
            ready, _, _ = select.select([master], [], [], min(1.0, remaining))
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data.decode("utf-8", errors="replace"))
            elif proc.poll() is not None:
                break
    finally:
        os.close(master)
        proc.wait()
    return "".join(chunks), proc.returncode
