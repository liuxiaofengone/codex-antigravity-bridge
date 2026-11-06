"""Private metadata persistence for asynchronous AGY jobs."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ALLOWED_FIELDS = {
    "job_id",
    "state",
    "process_control",
    "cancellation_support",
    "created_at",
    "updated_at",
    "workdir",
    "pid",
    "exit_code",
    "error_kind",
    "reason",
    "retryable",
    "conversation_id",
    "cli_status",
    "failed_command",
    "permission_resource",
    "capability_id",
    "finished_reason",
    "remote_termination",
    "remote_context_digest",
    "remote_evidence_refs",
    "remote_identity_status",
    "business_acceptance",
}


class JobStore:
    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            configured = os.environ.get("AGY_BRIDGE_STATE_DIR")
            state_dir = (
                Path(configured).expanduser()
                if configured
                else Path.home() / ".codex-agy-bridge"
            )
            root = state_dir / "jobs"
        self.root = Path(root).expanduser()

    def save(self, record: Mapping[str, Any]) -> None:
        job_id = str(record.get("job_id", ""))
        if not _JOB_ID.fullmatch(job_id):
            raise ValueError("job_id is unsafe")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        now = datetime.now(timezone.utc).isoformat()
        data = {
            key: _clean(value)
            for key, value in record.items()
            if key in _ALLOWED_FIELDS
        }
        data.setdefault("created_at", now)
        data["updated_at"] = now
        data.setdefault("remote_termination", "unknown")
        temp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=f".{job_id}-",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(temp.name)
        try:
            with temp:
                json.dump(data, temp, ensure_ascii=False, indent=2, sort_keys=True)
                temp.write("\n")
            try:
                temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(temp_path, self.root / f"{job_id}.json")
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def load(self, job_id: str) -> dict[str, Any] | None:
        if not _JOB_ID.fullmatch(job_id):
            return None
        path = self.root / f"{job_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(raw, dict) or raw.get("job_id") != job_id:
            return None
        return {
            key: value
            for key, value in raw.items()
            if key in _ALLOWED_FIELDS
        }


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return "".join(char for char in value if ord(char) >= 32 and ord(char) != 127)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)
