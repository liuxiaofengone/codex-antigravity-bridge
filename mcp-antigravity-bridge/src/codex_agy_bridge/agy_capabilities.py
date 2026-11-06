"""Side-effect-free discovery of the installed Antigravity CLI."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_MODE_NAMES = ("accept-edits", "auto", "plan")
_HELP_USAGE = re.compile(r"(?m)^Usage(?:\s+of\s+\S+)?:")
_DECLARED_FLAG = re.compile(
    r"(?m)^\s+(--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z])(?=\s|$)"
)
_VERSION = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")
_CACHE_LOCK = threading.RLock()
_CACHE_KEY: tuple[Any, ...] | None = None
_CACHE_VALUE: "CapabilitySnapshot | None" = None


class CapabilityDiscoveryError(RuntimeError):
    """Raised when the live CLI cannot be inspected safely."""


@dataclass(frozen=True)
class CapabilitySnapshot:
    capability_id: str
    binary_path: str
    cli_version: str
    python_platform: str
    declared_flags: dict[str, bool]
    mode_availability: dict[str, str]
    settings_summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clear_capability_cache() -> None:
    global _CACHE_KEY, _CACHE_VALUE
    with _CACHE_LOCK:
        _CACHE_KEY = None
        _CACHE_VALUE = None


def find_agy() -> str | None:
    """Resolve AGY lazily to avoid a runner/capability import cycle."""
    from .agy_runner import find_agy as resolve_agy

    return resolve_agy()


def _run_probe(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise CapabilityDiscoveryError(
            f"agy capability probe timed out: {Path(args[0]).name} {' '.join(args[1:])}"
        ) from exc
    except OSError as exc:
        raise CapabilityDiscoveryError(
            f"agy capability probe failed for {Path(args[0]).name}: {exc}"
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _home_dir() -> Path:
    return Path.home()


def _settings_paths() -> list[Path]:
    configured = os.environ.get("AGY_SETTINGS_ROOT")
    if configured:
        root = Path(configured).expanduser()
        return [
            root / "user_settings.pb",
            root / "antigravity_state.pbtxt",
            root / "mcp_config.json",
        ]
    home = _home_dir()
    if sys.platform == "win32":
        return [
            home / ".gemini" / "antigravity" / "user_settings.pb",
            home / ".gemini" / "antigravity" / "antigravity_state.pbtxt",
            home / ".gemini" / "antigravity" / "mcp_config.json",
            home / ".gemini" / "antigravity-cli" / "settings.json",
            home / ".gemini" / "config" / "config.json",
            home / ".gemini" / "config" / "mcp_config.json",
        ]
    return [
        home / ".config" / "antigravity" / "user_settings.pb",
        home / ".config" / "antigravity" / "antigravity_state.pbtxt",
        home / ".config" / "antigravity" / "mcp_config.json",
        home / ".gemini" / "antigravity-cli" / "settings.json",
        home / ".gemini" / "config" / "config.json",
        home / ".gemini" / "config" / "mcp_config.json",
    ]


def _settings_summary() -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    paths = _settings_paths()
    for path in paths:
        source: dict[str, Any] = {
            "name": path.name,
            "path": str(path),
            "format": path.suffix.lstrip(".").lower() or "opaque",
            "exists": path.is_file(),
            "size": None,
            "sha256": None,
        }
        if path.is_file():
            try:
                source["size"] = path.stat().st_size
                source["sha256"] = _sha256_file(path)
            except OSError:
                source["exists"] = False
        sources.append(source)
    encoded = json.dumps(sources, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return {
        "roots": list(dict.fromkeys(str(path.parent) for path in paths)),
        "sources": sources,
        "digest": hashlib.sha256(encoded).hexdigest(),
    }


def _binary_fingerprint(binary: Path) -> dict[str, Any]:
    try:
        info = binary.stat()
    except OSError as exc:
        raise CapabilityDiscoveryError(f"agy binary is not readable: {binary.name}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CapabilityDiscoveryError(f"agy binary is not a regular file: {binary.name}")
    return {
        "path": str(binary.resolve()),
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
    }


def _parse_flags(help_text: str) -> dict[str, bool]:
    return {
        flag: True
        for flag in sorted(set(_DECLARED_FLAG.findall(help_text)))
    }


def _probe_version(binary: Path) -> str:
    result = _run_probe([str(binary), "--version"], timeout=10)
    if result.returncode != 0:
        raise CapabilityDiscoveryError(
            f"agy --version failed with exit code {result.returncode}"
        )
    match = _VERSION.search(result.stdout)
    return match.group(1) if match else "unknown"


def _probe_help(binary: Path) -> str:
    result = _run_probe([str(binary), "--help"], timeout=10)
    if result.returncode != 0:
        raise CapabilityDiscoveryError(
            f"agy --help failed with exit code {result.returncode}"
        )
    help_text = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    )
    if not help_text.strip() or _HELP_USAGE.search(help_text) is None:
        raise CapabilityDiscoveryError("agy --help returned an unknown format")
    return help_text


def _python_platform() -> str:
    return (
        f"{platform.system()}-{platform.machine()}-"
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )


def _capability_id(
    binary_fingerprint: dict[str, Any],
    cli_version: str,
    declared_flags: dict[str, bool],
    python_platform: str,
    settings_digest: str,
) -> str:
    material = {
        "binary": binary_fingerprint,
        "cli_version": cli_version,
        "declared_flags": declared_flags,
        "python_platform": python_platform,
        "settings_digest": settings_digest,
    }
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def inspect_agy_capabilities(refresh: bool = False) -> CapabilitySnapshot:
    """Inspect only `agy --version` and `agy --help`.

    Declared flags are not runtime permission evidence. Mode and policy
    availability intentionally remain unknown until a separately authorized
    canary supplies local evidence.
    """
    global _CACHE_KEY, _CACHE_VALUE

    if not isinstance(refresh, bool):
        raise ValueError("refresh must be a boolean")
    binary_value = find_agy()
    if not binary_value:
        raise CapabilityDiscoveryError("agy binary not found")
    binary = Path(binary_value)
    binary_fingerprint = _binary_fingerprint(binary)
    settings = _settings_summary()
    cache_key = (
        binary_fingerprint["path"],
        binary_fingerprint["mtime_ns"],
        binary_fingerprint["size"],
        settings["digest"],
    )

    with _CACHE_LOCK:
        if not refresh and _CACHE_KEY == cache_key and _CACHE_VALUE is not None:
            return _CACHE_VALUE
        cli_version = _probe_version(binary)
        help_text = _probe_help(binary)
        declared_flags = _parse_flags(help_text)
        python_platform = _python_platform()
        capability_id = _capability_id(
            binary_fingerprint,
            cli_version,
            declared_flags,
            python_platform,
            settings["digest"],
        )
        snapshot = CapabilitySnapshot(
            capability_id=capability_id,
            binary_path=binary_fingerprint["path"],
            cli_version=cli_version,
            python_platform=python_platform,
            declared_flags=declared_flags,
            mode_availability={mode: "unknown" for mode in _MODE_NAMES},
            settings_summary=settings,
        )
        _CACHE_KEY = cache_key
        _CACHE_VALUE = snapshot
        return snapshot
