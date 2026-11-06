# codex-agy-bridge

This directory contains the Python package behind the local MCP bridge. The
user-facing installation and usage guide lives in the repository root:

- [English project guide](../README.md)
- [Chinese project guide](../README.zh-CN.md)
- [Documentation index](../docs/README.en.md)
- [Verification demo](../docs/demo.md)

## Local install

From the repository root:

```powershell
python -m pip install -e ".\mcp-antigravity-bridge[dev,winpty]"
codex-agy-bridge-setup --what-if
codex-agy-bridge-setup
```

On macOS or Linux, use the `dev` extra instead of `winpty`. The setup command
registers the current Python interpreter, installs the packaged supervisor
skill, and manages only this server's proxy variables. It does not read, save,
or print OAuth credentials.

Use `--proxy-url <url>` for an explicit local HTTP or SOCKS5 proxy, or
`--no-proxy` to remove the managed proxy settings. Proxy URLs with embedded
credentials are rejected.

## MCP tools

The server preserves the six original execution tools and adds five lifecycle,
diagnostic, and remote-contract tools:

- `agy_doctor`, `agy_policy_probe`, and `agy_preflight` for live capability,
  task-policy evidence, and contract checks;
- `agy_cancel` and `agy_remote_start` for managed local lifecycle and verified
  CNB/SSH task contracts;
- `agy_ask` and `agy_ask_json` for bounded synchronous calls;
- `agy_start` and `agy_status` for a caller-created isolated worktree;
- `agy_collab_start` and `agy_collab_status` for validated multi-worktree collaboration.

`dangerously_skip_permissions` defaults to `false`. Collaboration should start
with `dry_run=true`; the bridge never auto-merges branches or deletes worktrees.
Non-default `execution_options` require a task-local authorization reference
and passing local canary evidence. Static preflight never invokes AGY; active
preflight requires `AGY_PREFLIGHT_PROBE_OPT_IN=1`.

`agy_start` and `agy_collab_start` default to `process_control="legacy"`.
Explicit `managed` jobs are headless-only, use one stream-json process, expose
local cancellation, and persist redacted metadata. `agy_remote_start` manages
only the local coordinator process; `remote_termination` is always `unknown`.
On Windows, bridge-owned shell fixtures and `.ps1` shims use PowerShell, never
`cmd.exe`.

## Development checks

Run from this directory:

```powershell
python -m pytest -q
python -m compileall -q src
```

Tests mock the `agy` process boundary and do not require a live login.
