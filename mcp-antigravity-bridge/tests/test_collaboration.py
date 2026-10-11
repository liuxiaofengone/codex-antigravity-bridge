# Modified by liuxiaofengone, 2026-10-10: cover permission failure-kind propagation.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_agy_bridge.agy_collaboration import CollaborationRegistry


class FakeJobs:
    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.states: dict[str, dict[str, object]] = {}

    def start(self, prompt: str, **kwargs: object) -> str:
        job_id = f"job-{len(self.started) + 1}"
        self.started.append({"job_id": job_id, "prompt": prompt, **kwargs})
        self.states[job_id] = {
            "job_id": job_id,
            "state": "running",
        }
        return job_id

    def status(self, job_id: str) -> dict[str, object]:
        return self.states[job_id]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def test_start_creates_isolated_worktrees_and_jobs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    jobs = FakeJobs()
    registry = CollaborationRegistry(jobs=jobs)

    result = registry.start(
        project_dir=str(repo),
        tasks=[
            {
                "id": "backend",
                "role": "Backend",
                "prompt": "Implement the API.",
                "owned_paths": ["backend/"],
                "acceptance": ["backend tests pass"],
                "verification": ["python -m pytest"],
            },
            {
                "id": "frontend",
                "role": "Frontend",
                "prompt": "Implement the page.",
                "owned_paths": ["frontend/"],
                "acceptance": ["frontend tests pass"],
            },
        ],
        shared_contract="The frontend consumes /api/items.",
    )

    assert result["state"] == "running"
    assert result["merge_policy"] == "manual"
    assert len(result["tasks"]) == 2
    assert len(jobs.started) == 2
    for task in result["tasks"]:
        workdir = Path(task["workdir"])
        assert workdir.is_dir()
        assert task["branch"].startswith("codex-agy/")
        assert _git(workdir, "status", "--porcelain") == ""

    prompts = "\n".join(str(item["prompt"]) for item in jobs.started)
    assert "The frontend consumes /api/items." in prompts
    assert "Owned files: backend" in prompts
    assert "Owned files: frontend" in prompts


def test_status_aggregates_jobs_and_reports_worktree_checks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    jobs = FakeJobs()
    registry = CollaborationRegistry(jobs=jobs)
    started = registry.start(
        project_dir=str(repo),
        tasks=[
            {
                "id": "backend",
                "prompt": "Implement the API.",
                "owned_paths": ["backend/"],
                "acceptance": ["tests pass"],
            }
        ],
    )

    job_id = jobs.started[0]["job_id"]
    jobs.states[job_id] = {
        "job_id": job_id,
        "state": "completed",
        "text": "Implemented and tested.",
        "exit_code": 0,
        "used_pty": False,
    }
    task = started["tasks"][0]
    workdir = Path(task["workdir"])
    (workdir / "backend").mkdir()
    (workdir / "backend" / "api.py").write_text("print('ok')\n", encoding="utf-8")

    status = registry.status(started["session_id"])

    assert status["state"] == "ready_for_review"
    assert status["tasks"][0]["state"] == "completed"
    assert status["tasks"][0]["worktree"]["dirty"] is True
    assert status["tasks"][0]["worktree"]["diff_check"] == "passed"
    assert status["tasks"][0]["acceptance_status"] == "manual"
    assert status["scope_status"] == "passed"
    assert status["changed_files"] == ["backend/api.py"]


def test_dry_run_validates_without_creating_worktrees_or_jobs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    jobs = FakeJobs()
    registry = CollaborationRegistry(jobs=jobs)
    planned_root = tmp_path / "planned-worktrees"

    result = registry.start(
        project_dir=str(repo),
        worktree_root=str(planned_root),
        dry_run=True,
        shared_contract="frontend consumes /api/items",
        tasks=[
            {
                "id": "backend",
                "prompt": "Implement the API.",
                "owned_paths": ["backend"],
                "acceptance": ["tests pass"],
                "verification": ["python -m pytest"],
            }
        ],
    )

    assert result["state"] == "dry-run"
    assert result["session_id"]
    assert result["shared_contract"] == "frontend consumes /api/items"
    assert result["tasks"][0]["branch"].startswith("codex-agy/")
    assert result["tasks"][0]["owned_paths"] == ["backend"]
    assert not planned_root.exists()
    assert jobs.started == []
    assert registry.status(result["session_id"])["state"] == "unknown"


def test_status_audits_committed_uncommitted_untracked_and_deleted_files(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    jobs = FakeJobs()
    registry = CollaborationRegistry(jobs=jobs)
    started = registry.start(
        project_dir=str(repo),
        tasks=[
            {
                "id": "backend",
                "prompt": "Implement the API.",
                "owned_paths": ["backend"],
                "acceptance": ["tests pass"],
            }
        ],
    )

    job_id = jobs.started[0]["job_id"]
    jobs.states[job_id] = {
        "job_id": job_id,
        "state": "completed",
        "text": "Implemented and tested.",
        "exit_code": 0,
        "used_pty": False,
    }
    workdir = Path(started["tasks"][0]["workdir"])
    backend = workdir / "backend"
    backend.mkdir()
    (backend / "committed.py").write_text("COMMITTED = True\n", encoding="utf-8")
    (backend / "modified.py").write_text("VALUE = 1\n", encoding="utf-8")
    (backend / "deleted.py").write_text("DELETE_ME = True\n", encoding="utf-8")
    _git(workdir, "add", "backend")
    _git(workdir, "commit", "-m", "add backend files")
    (backend / "modified.py").write_text("VALUE = 2\n", encoding="utf-8")
    (backend / "deleted.py").unlink()
    (backend / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")
    (workdir / "outside.py").write_text("OUTSIDE = True\n", encoding="utf-8")

    status = registry.status(started["session_id"])
    worktree = status["tasks"][0]["worktree"]

    assert "backend/committed.py" in worktree["committed"]
    assert "backend/modified.py" in worktree["uncommitted"]
    assert "backend/untracked.py" in worktree["untracked"]
    assert "backend/deleted.py" in worktree["deleted"]
    assert worktree["scope_status"] == "violated"
    assert worktree["scope_violations"] == ["outside.py"]
    assert status["scope_status"] == "violated"
    assert status["scope_violations"] == ["outside.py"]


def test_start_rejects_overlapping_owned_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    registry = CollaborationRegistry(jobs=FakeJobs())

    with pytest.raises(ValueError, match="overlap"):
        registry.start(
            project_dir=str(repo),
            tasks=[
                {
                    "id": "one",
                    "prompt": "One",
                    "owned_paths": ["src/"],
                    "acceptance": ["pass"],
                },
                {
                    "id": "two",
                    "prompt": "Two",
                    "owned_paths": ["src/ui/"],
                    "acceptance": ["pass"],
                },
            ],
        )


def test_start_rejects_absolute_and_unc_owned_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    registry = CollaborationRegistry(jobs=FakeJobs())

    for path in ("/backend", r"\server\share"):
        with pytest.raises(ValueError, match="relative paths"):
            registry.start(
                project_dir=str(repo),
                tasks=[
                    {
                        "id": "backend",
                        "prompt": "Implement the API.",
                        "owned_paths": [path],
                        "acceptance": ["pass"],
                    }
                ],
            )


def test_status_reports_both_ends_of_uncommitted_rename(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "backend").mkdir()
    (repo / "backend" / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "backend")
    _git(repo, "commit", "-m", "add backend file")
    jobs = FakeJobs()
    registry = CollaborationRegistry(jobs=jobs)
    started = registry.start(
        project_dir=str(repo),
        tasks=[
            {
                "id": "backend",
                "prompt": "Rename the backend file.",
                "owned_paths": ["backend"],
                "acceptance": ["rename is reviewed"],
            }
        ],
    )

    job_id = jobs.started[0]["job_id"]
    jobs.states[job_id] = {"job_id": job_id, "state": "completed", "exit_code": 0}
    workdir = Path(started["tasks"][0]["workdir"])
    _git(workdir, "mv", "backend/old.py", "outside.py")

    status = registry.status(started["session_id"])
    worktree = status["tasks"][0]["worktree"]

    assert "backend/old.py" in worktree["changed_files"]
    assert "outside.py" in worktree["changed_files"]
    assert status["scope_status"] == "violated"
    assert "outside.py" in status["scope_violations"]


def test_start_rejects_nonfinite_timeout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    registry = CollaborationRegistry(jobs=FakeJobs())

    with pytest.raises(ValueError, match="timeout"):
        registry.start(
            project_dir=str(repo),
            timeout=float("nan"),
            tasks=[
                {
                    "id": "backend",
                    "prompt": "Implement the API.",
                    "owned_paths": ["backend"],
                    "acceptance": ["pass"],
                }
            ],
        )


def test_start_rejects_more_tasks_than_the_session_limit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    registry = CollaborationRegistry(jobs=FakeJobs())

    task = {
        "id": "one",
        "prompt": "One",
        "owned_paths": ["one"],
        "acceptance": ["pass"],
    }
    with pytest.raises(ValueError, match="max_tasks"):
        registry.start(project_dir=str(repo), tasks=[task, {**task, "id": "two"}], max_tasks=1)


def test_status_reports_unknown_session() -> None:
    registry = CollaborationRegistry(jobs=FakeJobs())

    assert registry.status("missing") == {
        "session_id": "missing",
        "state": "unknown",
        "error": "collaboration session not found",
    }


def test_status_preserves_permission_failure_kind_with_zero_exit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    jobs = FakeJobs()
    registry = CollaborationRegistry(jobs=jobs)
    started = registry.start(project_dir=str(repo), tasks=[{
        "id": "readiness", "prompt": "Fixture only.", "owned_paths": ["README.md"],
        "acceptance": ["Report the actual result."],
    }])
    job_id = jobs.started[0]["job_id"]
    jobs.states[job_id] = {
        "job_id": job_id, "state": "failed", "exit_code": 0,
        "error_kind": "permission", "error": "AGY_PERMISSION_DENIED: RunCommand",
    }
    status = registry.status(started["session_id"])
    assert status["state"] == "failed"
    assert status["tasks"][0]["exit_code"] == 0
    assert status["tasks"][0]["error_kind"] == "permission"