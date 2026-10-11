"""Real MCP stdio smoke test; no Antigravity login is required."""

# Modified by liuxiaofengone, 2026-10-08: add a real MCP collaboration dry-run regression test.
# Updated 2026-10-10: cover soft-denied CLI results over the real MCP transport.

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


async def _list_tools() -> set[str]:
    env = dict(os.environ)
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(SOURCE), current_pythonpath) if value
    )
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_agy_bridge"],
        cwd=ROOT,
        env=env,
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=15)
            result = await asyncio.wait_for(session.list_tools(), timeout=15)
            return {tool.name for tool in result.tools}


def test_mcp_stdio_server_lists_all_tools() -> None:
    assert asyncio.run(_list_tools()) == {
        "agy_ask",
        "agy_ask_json",
        "agy_start",
        "agy_status",
        "agy_collab_start",
        "agy_collab_status",
    }


async def _collaboration_dry_run(repo: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_agy_bridge"],
        cwd=ROOT,
        env=env,
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=15)
            result = await asyncio.wait_for(
                session.call_tool("agy_collab_start", {
                    "project_dir": str(repo),
                    "dry_run": True,
                    "max_tasks": 1,
                    "tasks": [{
                        "id": "smoke",
                        "prompt": "Dry-run validation only.",
                        "owned_paths": ["README.md"],
                        "acceptance": ["Return a plan without creating worktrees."],
                    }],
                }),
                timeout=5,
            )
            assert not result.isError
            return json.loads(result.content[0].text)


def test_mcp_stdio_collaboration_dry_run_returns_without_reading_transport(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("smoke test\n", encoding="utf-8")
    for args in (
        ("init",),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test User"),
        ("add", "README.md"),
        ("commit", "-m", "initial"),
    ):
        subprocess.run(["git", *args], cwd=repo, stdin=subprocess.DEVNULL,
                       capture_output=True, check=True)
    result = asyncio.run(_collaboration_dry_run(repo))
    assert result["state"] == "dry-run"
    assert result["task_count"] == 1
    assert not Path(result["worktree_root"]).exists()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo,
                                     stdin=subprocess.DEVNULL)
    assert status == b""

async def _permission_failure_round_trip(tmp_path: Path) -> None:
    # Exercise the actual MCP transport with a deterministic local CLI stand-in.
    # No real AGY login, command permission, or model request is involved.
    payload = json.dumps({
        "conversation_id": "test-conversation",
        "status": "SUCCESS",
        "response": "",
        "denied_actions": ["RunCommand"],
    })
    if os.name == "nt":
        binary = tmp_path / "agy.cmd"
        binary.write_text("@echo off\necho " + payload + "\nexit /b 0\n", encoding="utf-8")
    else:
        binary = tmp_path / "agy"
        binary.write_text("#!/bin/sh\nprintf '%s\\n' '" + payload + "'\n", encoding="utf-8")
        binary.chmod(0o700)
    env = dict(os.environ, AGY_PATH=str(binary), PYTHONPATH=str(SOURCE))
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "codex_agy_bridge"], cwd=ROOT, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=15)
            for name in ("agy_ask", "agy_ask_json"):
                result = await asyncio.wait_for(session.call_tool(name, {
                    "prompt": "Fixture only.", "workdir": str(tmp_path), "timeout": 5,
                }), timeout=10)
                assert result.isError
                assert "AGY_PERMISSION_DENIED" in result.content[0].text
            started = await session.call_tool("agy_start", {
                "prompt": "Fixture only.", "workdir": str(tmp_path), "timeout": 5,
            })
            assert not started.isError
            job_id = started.content[0].text
            for _ in range(100):
                result = await session.call_tool("agy_status", {"job_id": job_id})
                status = json.loads(result.content[0].text)
                if status["state"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.05)
            assert status["state"] == "failed"
            assert status["error_kind"] == "permission"
            assert status["exit_code"] == 0


def test_mcp_stdio_soft_permission_failure_is_error(tmp_path: Path) -> None:
    asyncio.run(_permission_failure_round_trip(tmp_path))