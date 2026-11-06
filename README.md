# Codex AGY Supervisor

> Modified distribution maintained by **liuxiaofengone**, 2026-10-08.
> Based on [crazyzhang277/codex-antigravity-bridge](https://github.com/crazyzhang277/codex-antigravity-bridge)
> (`https://github.com/crazyzhang277/codex-antigravity-bridge.git`), retaining its Git history and Apache-2.0 license.
> Changes isolate Git subprocess input from MCP stdio, add a regression test, and install dependencies before setup preview.
> See the [Windows installation and delegation guide](docs/windows-setup.zh-CN.md).

Codex plans and reviews. Antigravity implements bounded tasks in isolated Git worktrees.

A local MCP bridge that lets Codex delegate independent coding tasks to the
`agy` CLI, inspect the results, and keep the final merge decision with a human.

[Quick Start](#quick-start) | [Demo](docs/demo.md) | [Security](SECURITY.md) | [中文说明](README.zh-CN.md)

## Why this project

Most bridges stop at starting `agy`. This project focuses on the supervision
boundary around a coding task:

- each task declares its owned paths;
- each task can run in its own Git worktree;
- shared contracts, acceptance criteria, and verification commands are explicit;
- status includes changed files and scope-audit results;
- the bridge never auto-merges or silently expands task scope.

## Is this for you?

Use it when you already use Codex and the Antigravity CLI and want to delegate
independent coding tasks without letting workers share the same worktree.

This is not a hosted agent platform, a replacement for Codex, an Antigravity
desktop controller, or an automatic merge bot.

## Quick Start

Prerequisites:

- Python 3.10 or newer;
- Git;
- Codex Desktop or Codex CLI with MCP support;
- Antigravity CLI installed as `agy`;
- one interactive `agy` login before the first real task.

### Source install

```powershell
git clone https://github.com/liuxiaofengone/codex-antigravity-bridge.git
cd codex-antigravity-bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\mcp-antigravity-bridge[winpty]"
.\.venv\Scripts\python.exe -m codex_agy_bridge.setup --what-if
.\.venv\Scripts\python.exe -m codex_agy_bridge.setup
```

On macOS or Linux:

```bash
git clone https://github.com/liuxiaofengone/codex-antigravity-bridge.git
cd codex-antigravity-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -e "./mcp-antigravity-bridge"
.venv/bin/python -m codex_agy_bridge.setup --what-if
.venv/bin/python -m codex_agy_bridge.setup
```

The setup command installs the packaged `agy-supervisor` skill, registers the
MCP server with the current Python interpreter, and updates only the managed
proxy variables for this server. It does not read, save, or print OAuth tokens.

### Package install

These are the intended release paths once `codex-agy-bridge` is available from
the package index:

```powershell
pipx install codex-agy-bridge
codex-agy-bridge-setup

uv tool install codex-agy-bridge
codex-agy-bridge-setup
```

For a local editable install while developing the bridge:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\mcp-antigravity-bridge[dev,winpty]"
.\.venv\Scripts\python.exe -m codex_agy_bridge.setup --what-if
```

Complete an interactive login before the real connectivity check:

```powershell
agy --version
agy
agy -p "Reply exactly AGY_OK"
codex mcp list
```

Use `AGY_PROXY_URL`, `PROXY_URL`, or `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`
for a proxy. An explicit setup proxy can be passed as
`.\.venv\Scripts\python.exe -m codex_agy_bridge.setup --proxy-url http://127.0.0.1:7890`; use
`--no-proxy` to remove the managed proxy entries. URLs containing credentials
are rejected so they cannot be copied into Codex configuration.

## Runtime Modes

| Mode | Entry point | Worktree | Best for |
| --- | --- | --- | --- |
| Normal Codex development | No agy tool | Current worktree | Ordinary local development |
| Synchronous delegation | `agy_ask` / `agy_ask_json` | Caller-selected | One bounded analysis task |
| Async isolated task | `agy_start` / `agy_status` | Caller-created | One independent implementation task |
| Collaboration MVP | `agy_collab_start` / `agy_collab_status` | Bridge-created | Independent frontend, backend, or test tracks |

`headless` is the default display mode. `terminal` opens one visible Windows
console per collaboration task. A session is capped at four tasks.

## Thirty-second workflow

Start with `dry_run=true` to validate the repository, base ref, task contract,
owned-path overlap, branches, worktree paths, and acceptance metadata without
starting `agy` or creating a worktree:

```json
{
  "project_dir": "C:/work/my-app",
  "base_ref": "HEAD",
  "dry_run": true,
  "shared_contract": "Frontend consumes GET /api/items with id and name.",
  "display_mode": "headless",
  "tasks": [
    {
      "id": "backend",
      "prompt": "Implement GET /api/items and its tests.",
      "owned_paths": ["backend/"],
      "acceptance": ["API tests pass"],
      "verification": ["python -m pytest backend"]
    },
    {
      "id": "frontend",
      "prompt": "Implement the items page against the shared contract.",
      "owned_paths": ["frontend/"],
      "acceptance": ["Frontend tests pass"]
    }
  ]
}
```

Run the same request with `dry_run=false` only after the plan is clear. Poll
`agy_collab_status(session_id)` and review each task's branch, changed files,
uncommitted files, `scope_status`, and `scope_violations`. `ready_for_review`
means only that the process exited successfully; it is not acceptance proof.

## Tools

| Tool | Purpose |
| --- | --- |
| `agy_doctor` | Read-only live CLI capability, architecture, settings digest, and policy evidence snapshot |
| `agy_policy_probe` | Explicit local canary for session-settings isolation and global-file stability |
| `agy_preflight` | Five-minute task-contract validation with side-effect-free and explicit active modes |
| `agy_cancel` | Cancel a queued legacy job or a managed local process tree |
| `agy_remote_start` | Start a managed local job bound to a verified CNB/SSH development context |
| `agy_ask` | One bounded synchronous CLI task |
| `agy_ask_json` | One task with validated JSON output |
| `agy_start` | Async work in a caller-created isolated worktree |
| `agy_status` | Poll an async job |
| `agy_collab_start` | Validate a contract and start up to four isolated tasks |
| `agy_collab_status` | Aggregate task results and changed-file scope audits |

The six execution tool names and default behavior remain stable. Optional
`execution_options` add exact task-scoped commands only after private local
canary evidence exists. Active policy or preflight probes require their explicit
environment opt-ins, and model prose is never accepted as tool-execution proof.

`agy_start` and `agy_collab_start` default to `legacy`. Explicit `managed`
execution is headless-only, uses a single stream-json process, supports local
process-tree cancellation, and never retries through a PTY. `agy_remote_start`
uses the local coordinator directory as cwd and always reports
`remote_termination="unknown"`. Windows script shims and bridge-owned test
commands use PowerShell, never `cmd.exe`.

## Security and limits

- communication stays on local MCP stdio;
- `dangerously_skip_permissions` defaults to `false`;
- the bridge does not store or forward OAuth credentials;
- the bridge does not auto-merge, delete worktrees, or execute arbitrary verification commands;
- scope violations are reported, not silently reverted;
- `ready_for_review` does not mean that acceptance criteria passed;
- production, irreversible, cross-project, and unclear tasks remain out of scope.

See [SECURITY.md](SECURITY.md) for reporting guidance and [docs/demo.md](docs/demo.md)
for a reproducible dry-run and live-demo checklist.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\mcp-antigravity-bridge[dev,winpty]"
python -m pytest -q
python scripts/validate_skill.py
python -m compileall -q mcp-antigravity-bridge/src
python -m build mcp-antigravity-bridge
git diff --check
```

Tests mock the `agy` process boundary and do not require a live login.

## Documentation

- [中文项目首页](README.zh-CN.md)
- [Demo and verification checklist](docs/demo.md)
- [Documentation index](docs/README.en.md)
- [Supervisor skill](skills/agy-supervisor/SKILL.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

Apache-2.0
