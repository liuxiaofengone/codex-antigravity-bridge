# Changelog

## 0.3.0

- Added explicit `legacy` and `managed` asynchronous job lifecycles while preserving the legacy default.
- Added local process-tree cancellation, `agy_cancel`, restart-safe private job metadata, and a PowerShell-only Windows test and script-shim path.
- Added explicit CNB/SSH task contracts through `agy_remote_start`; remote POSIX paths are never used as local cwd and remote termination remains unknown.

## 0.2.0

- Added side-effect-free `agy_doctor` capability discovery for the live AGY CLI and relevant settings.
- Added task-scoped `execution_options`, exact-command session rules, private 24-hour policy evidence, and `agy_policy_probe`.
- Preserved structured permission, conversation, failed-command, retryability, and redacted JSON failure details across sync, async, and collaboration results.
- Added `agy_preflight` task contracts with five-minute bindings, static checks, explicit active probes, and strict separation of CLI tool evidence from model self-report.

## Unreleased

- Consolidated user documentation around the root READMEs, docs index, and demo.
- Replaced the long package runtime manual with a short package-level install note.
- Documented current proxy setup, dry-run collaboration checks, and verification counts.

## 0.1.0

- Added dry-run collaboration planning without worktree or job side effects.
- Added committed, uncommitted, untracked, deleted, and owned-path scope audits.
- Added the packaged `agy-supervisor` resource and `codex-agy-bridge-setup` command.
- Added a concise English project homepage, demo checklist, security policy, and contribution guide.
