---
name: agy-supervisor
description: Use when the user explicitly asks Codex to involve Antigravity, agy, or another coding agent, or enables supervisor mode for bounded implementation and multi-page development tasks. Keep Codex as the reviewer and orchestrator while agy performs scoped implementation.
---

# AGY Supervisor Skill

## Normal mode

Do not call `agy_ask`, `agy_ask_json`, `agy_start`, or `agy_collab_start` during ordinary
development. Use delegation only when the user explicitly asks for Antigravity
involvement or explicitly opts into Supervisor mode.

## Supervisor mode

Use this sequence:

1. Inspect the repository and requested scope.
2. Call `agy_doctor` and compare the live CLI capability with the task.
3. Define the workdir, risk class, authorization, and exclusive file ownership.
4. Build a task contract and call `agy_preflight`.
5. Build the required delegation prompt and choose the permission mode.
6. Call `agy_ask`, `agy_ask_json`, `agy_start`, or the collaboration MVP.
7. Inspect status, diff, worktree boundaries, output, and tests.
8. Accept only a verified success; otherwise make at most two evidence-based
   corrective calls, then stop with the exact blocker.

Every delegation prompt must include task scope, owned files, forbidden files,
acceptance criteria, verification commands, permission mode, authorization, and
the requested report fields. The copyable template, risk matrix, result state
machine, lifecycle checklist, correction protocol, and pressure cases are in
[`references/agy-supervisor-protocol.md`](references/agy-supervisor-protocol.md).
Parallel worktree plans live under `docs/agy-plans/` and start with
`Status: READY_FOR_AGY`; create and validate the caller-owned AGY worktree,
pass it as `workdir` to `agy_start`, and inspect `git worktree list` before and
after delegation. The bridge does not create worktrees for `agy_start`.

### Tool and output rules

- Apply the same scope and permission contract to `agy_ask`, `agy_ask_json`,
  `agy_start`, and `agy_collab_start`; `agy_ask_json` additionally requires parseable JSON matching the
  requested output schema.
- `agy_doctor` is side-effect-free. It runs only `agy --version` and
  `agy --help`, and declared flags are not runtime permission proof.
- Use `agy_policy_probe` only after exact authorization and
  `AGY_POLICY_PROBE_OPT_IN=1`. Passing evidence is local, private, scoped to the
  current CLI/settings fingerprint, and expires after 24 hours.
- Non-default `execution_options` require `authorization_ref`; commands are
  rendered as exact `command(<command>)` selectors. Do not broaden a task into
  `command(git)`, `command(ssh)`, `command(cnb)`, or a wildcard.
- Run `agy_preflight` before implementation. Static preflight never invokes the
  model. An active probe requires `AGY_PREFLIGHT_PROBE_OPT_IN=1`, submits at most
  one bounded request, and accepts only CLI tool events as ready evidence.
- A five-minute preflight report binds task, capability, and policy digests. It
  is not an execution lock; reread worktree, branch, and baseline identity
  before changing files.
- `agy_start` requires an existing caller-created isolated worktree as `workdir`;
  an empty or non-directory workdir is rejected.
- `dangerously_skip_permissions=false` is the default. Enable it only after
  explicit authorization for the exact trusted worktree and task.
- `agy_status` may report `queued`, `running`, `completed`, `failed`, or
  `unknown`. An `unknown` job is not a success: preserve the worktree, inspect
  it manually, and do not restart blindly.
- A result is `succeeded` only with usable output, acceptable exit status, no
  permission/authentication error, and passing acceptance criteria.
- There are three total AGY calls per delegated task: one initial call and at
  most two corrective calls. Never issue a fourth call or widen ownership.

## Collaboration mode MVP

Use this mode only after the user explicitly requests Codex and AGY to develop
the same project in parallel. It is appropriate for independently implementable
tasks such as a frontend/backend split, not for shared routing, authentication,
database migrations, or concurrent edits to one file.

Call `agy_collab_start` with the repository root, a shared contract, and a task
list. Every task must declare `id`, `prompt`, `owned_paths`, and `acceptance`;
owned paths must be mutually exclusive. The bridge creates a temporary branch
and worktree for each task, then starts bounded AGY jobs. Codex may continue
working in its own worktree while those jobs run.

Before starting a collaboration session, ask the user once:

```text
是否需要打开实时终端显示 agy 的执行过程？默认：否。
本次准备派几个独立任务给 agy？默认：1，最多：4。
```

If the user gives no choice, use `display_mode="headless"` and one task. Do
not start more than four tasks in one session. Use `display_mode="terminal"`
only on Windows and only after the user opts into visible output; it opens one
terminal window per running task and still requires the normal diff and test
review.

Poll `agy_collab_status` until the session is `ready_for_review` or `failed`.
`ready_for_review` means only that AGY exited successfully. Codex must still
inspect each branch, run the acceptance checks, review `diff_check` and
uncommitted changes, and merge manually. The MVP never auto-merges, deletes
worktrees, or executes arbitrary verification commands.

### Hard boundaries

Do not delegate unresolved shared state, routing, authentication, database,
production, secrets, irreversible operations, concurrent writes in one
worktree, or unknown workdirs. Destructive and production work is blocked by
default; production operations remain forbidden even with full access.

## Authorization checkpoint

Ask once whether to enable AGY collaboration when the task is substantial,
needs at least two independently implementable pages/modules, or benefits from
a second implementation track and the user has not already opted in. Do not
ask for trivial fixes, read-only analysis, secrets, production operations, or
irreversible actions. If the user declines, continue in Normal mode.

## Parallel worktree gate

Use parallel mode only when shared contracts are known, file boundaries are
exclusive, and Codex and AGY can work in different worktrees. Create and commit
the plan under `docs/agy-plans/`. For the MVP, `agy_collab_start` may create the
temporary worktrees; otherwise create the caller-owned worktree and use
`agy_start`. Poll with `agy_collab_status` or `agy_status`, and merge only after
the post-delegation audit and acceptance criteria pass. Use `agy_ask` only as a
synchronous fallback when asynchronous tools are unavailable.

Stop when acceptance criteria and tests pass, there is no meaningful progress,
scope changes, a permission/authentication blocker appears, a timeout occurs,
or a user decision is required.

Never store OAuth tokens or private machine configuration. See the reference
protocol before every delegated implementation and the plan template before
parallel worktree collaboration.
