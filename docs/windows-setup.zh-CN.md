# Windows 安装与 AGY 委派指南

本指南适用于 liuxiaofengone 维护的派生版本。原项目由 crazyzhang277 开发，采用 Apache-2.0 许可证；来源与本地修改见仓库根目录的 NOTICE。

## 环境要求

- 自行安装的 Python 3.10 或以上版本、Git。
- 支持 MCP 的 Codex Desktop 或 Codex CLI，且 `codex` 可以在终端运行。
- Antigravity CLI，且 `agy` 可以在终端运行。
- 首次真实调用前，使用 `agy` 完成交互式登录。

优先使用本机 ARM64 Python。若 Python 3.14 ARM64 在安装最新
`cryptography` 时尝试从源码编译并报缺少 OpenSSL，可先安装仍提供
`cp311-abi3-win_arm64` 预编译 wheel 的
`cryptography==46.0.3`，再安装本项目。只有原生 wheel 链确实不可用时才
考虑 x64 环境；那会丢失原生 ARM64 运行时优势。当前已验证的独立安装命令：

```powershell
.\.venv\Scripts\python.exe -m pip install "cryptography==46.0.3"
.\.venv\Scripts\python.exe -m pip install -e ".\mcp-antigravity-bridge[winpty]"
```

## 1. 从源码建立独立环境

在自己选择的目录中运行：

```powershell
git clone https://github.com/liuxiaofengone/codex-antigravity-bridge.git
cd codex-antigravity-bridge
python -c "import sys; print(sys.executable); print(sys.version)"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\mcp-antigravity-bridge[winpty]"
```

如果需要选择其他 Python，把下面的示例路径替换为本机真实路径，再建立环境：

```powershell
$pythonExe = "C:\path\to\python.exe"
& $pythonExe -m venv .venv
```

命令直接调用独立环境的 Python，无需激活环境，也无需修改系统执行策略。`winpty` 用于 Windows 下终端输出回退。

## 2. 预览并注册 MCP

先安装依赖，再运行预览，避免首次安装时出现 `ModuleNotFoundError: mcp`。

```powershell
.\.venv\Scripts\python.exe -m codex_agy_bridge.setup --what-if
.\.venv\Scripts\python.exe -m codex_agy_bridge.setup
codex mcp get codex-agy-bridge
```

安装器会复制 `agy-supervisor` 技能，注册 MCP，并使用当前独立环境 Python 的真实绝对路径。配置默认位于 `%USERPROFILE%\.codex\config.toml`；设置了 `CODEX_HOME` 时使用相应目录。

需要代理时，传入自己的地址：

```powershell
.\.venv\Scripts\python.exe -m codex_agy_bridge.setup --proxy-url "http://127.0.0.1:PORT"
```

将 `PORT` 替换为实际端口。代理地址不得包含用户名或密码。需要移除本服务管理的代理条目时，使用 `--no-proxy`。

自动注册后，可以在本服务的 `[mcp_servers.codex-agy-bridge]` 配置节补充：

```toml
startup_timeout_sec = 120
tool_timeout_sec = 1200
```

安装器负责注册解释器和代理条目；这两个超时值来自配置示例，需要按任务耗时自行设置。单次同步任务默认超时为 300 秒，协作任务默认超时为 900 秒，因此可将外层工具等待设置为 1200 秒。

手动配置请参考 `mcp-antigravity-bridge/examples/codex-config.toml`。其中解释器路径是占位示例，必须替换。只合并本服务的配置节，不覆盖整个个人配置文件。

## 3. 登录并验证真实连接

```powershell
agy --version
agy
agy -p "Reply exactly AGY_OK"
```

完成登录后重启 Codex，再打开项目聊天，发送：

> 只调用一次 agy_ask，在当前项目中回复 MCP_AGY_OK。任务只读，不修改文件、不运行命令。

应收到可用回复。只有 MCP 注册成功还不足以证明 AGY 登录和网络连接正常。

当前功能版本为 `0.3.0`。重启 Codex 后先调用 `agy_doctor`：它只执行
`agy --version` 和 `agy --help`，并回读相关设置文件的摘要，不发送模型
prompt。`declared_flags` 只表示本机帮助声明了参数，执行模式和沙箱仍保持
`unknown`，直到本机 canary 提供证据。

## 4. 在聊天中委派

普通开发请求不会自动调用 AGY。明确写出任务范围、允许修改的文件、验收标准，以及是否允许合并。

### 只读检查

> 让 agy 只读检查当前项目的登录流程，列出潜在问题和修改建议。不要修改文件。你收到结果后检查建议是否成立。

### 一个实现任务

> 开启 AGY Supervisor，把用户设置页交给 agy。先明确允许修改的文件，并创建独立 Git 工作区。只修改设置页及其专属组件；公共组件、路由、登录逻辑和接口的改动需要先说明。要求昵称保存功能可用，成功和失败都有提示，相关测试通过。完成后由你审查差异、运行测试，暂不合并。

### 与 Codex 并行协作

> 开启 Codex 与 agy 协同开发。你负责后端接口，agy 负责前端页面。先确定接口契约和互不重叠的文件范围。给 agy 派 1 个任务，不打开实时终端。先运行协作预检，再开始实际工作。完成后汇总改动和测试结果，暂不合并。

原六个执行工具保持不变：`agy_ask` 和 `agy_ask_json` 用于同步请求；
`agy_start` 和 `agy_status` 用于调用方提供独立工作区的异步任务；
`agy_collab_start` 和 `agy_collab_status` 用于自动创建工作区的协作任务。

新增五个生命周期、诊断与远端合同工具：

- `agy_doctor` 只读检查 CLI 参数、Python/架构、相关设置摘要和已有策略证据；
- `agy_policy_probe` 在显式授权后运行三次本地 canary，验证会话隔离、
  deny/ask 保留和全局设置不变；
- `agy_preflight` 校验任务合同。静态预检不调用模型；主动预检最多提交一次
  受限命令检查。
- `agy_cancel` 取消排队 legacy job，或取消 managed 本地进程树；
- `agy_remote_start` 把已核验的 CNB/SSH 开发上下文绑定到本地 managed job。

`dangerously_skip_permissions` 默认关闭。`ready_for_review` 表示桥接器收到了可用输出，且未检测到进程、权限或回复错误；仍需审查改动范围并运行验收测试。

### 任务级执行选项

需要给单个任务增加具体命令许可时，四个执行入口都可传入
`execution_options`：

```json
{
  "mode": "accept-edits",
  "approved_commands": ["git status --short"],
  "authorization_ref": "用户于 2026-11-06 批准的 Issue 任务"
}
```

桥只构造 `command(<完整命令>)` 精确规则，并继承已有 `allow`、`deny`、
`ask`。它不接受任意 settings JSON，也不会把 `git`、`ssh` 或 `cnb` 扩成宽
前缀。非空选项必须有 `authorization_ref`，且不能与
`dangerously_skip_permissions=true` 同时使用。

首次启用或 CLI/设置摘要变化后，先完成显式 canary：

```powershell
$env:AGY_POLICY_PROBE_OPT_IN = "1"
```

然后在 Codex 中调用：

```text
agy_policy_probe(
  authorization_ref="用户于 2026-11-06 批准的本地 AGY 策略验证"
)
```

探针在临时 Git 仓库中只执行唯一 PowerShell `Write-Output` fixture，并分别
验证授权、未授权阻断和 deny 优先级。Windows 测试和桥自带脚本 shim 使用
PowerShell，不调用 `cmd.exe`。通过证据保存在 `%USERPROFILE%\.codex-agy-bridge\`
（或 `AGY_BRIDGE_STATE_DIR`），有效期 24 小时；失败时保持
`policy_unverified`，不要转向修改全局设置。

### 任务预检合同

静态预检示例：

```json
{
  "task_id": "issue-44-readonly-check",
  "capability_id": "<agy_doctor 返回的 capability_id>",
  "execution_target": "local",
  "local_dir": "C:/work/project",
  "mode": "accept-edits",
  "approved_commands": ["git status --short"],
  "authorization_ref": "用户批准的只读检查",
  "owned_paths": [],
  "forbidden_paths": ["src", "production"],
  "preflight_commands": ["git status --short"]
}
```

调用 `agy_preflight(contract=..., active_probe=false)` 只做静态校验，不启动
AGY。报告绑定合同摘要、CLI 能力和策略摘要，5 分钟后失效。

主动探针需要再次显式设置：

```powershell
$env:AGY_PREFLIGHT_PROBE_OPT_IN = "1"
```

`active_probe=true` 只提交一次请求，并要求 stream-json 中的
`step_update.step_type=tool`、`state=DONE` 和对应工具结果。模型自报、意图
回复或缺失工具事件只能是 `unknown`；权限拒绝为 `blocked`；要求桥提供硬性
工具拦截时返回 `unsupported_enforcement`。预检 ready 不等于业务开工成功，
远端任务仍需重新回读工作区、分支和基线 SHA。

### Managed 作业与取消

`agy_start` 和 `agy_collab_start` 默认保持 `process_control="legacy"`。需要
可取消的本地长任务时显式传入：

```json
{
  "process_control": "managed",
  "display_mode": "headless"
}
```

managed 模式只支持 headless，每次使用一个 `stream-json` 进程，不启用长期
输入队列，也不在空输出时自动 PTY 重跑。状态会包含：

- `process_control`
- `cancellation_support=local_process_tree`
- `termination_requested`
- `termination_confirmed`
- `job_object_assigned`
- `remote_termination=unknown`

Windows 优先把本地进程放入 Job Object。如果宿主 Job 限制导致无法分配嵌套
Job Object，桥只枚举并结束当前 job 自己的 PID 进程树，不按 `python`、
`agy` 或 `ssh` 名称全局清理；无法证明整棵树退出时仍返回
`termination_confirmed=false`。

取消调用：

```text
agy_cancel(job_id="<job id>", wait_timeout=10)
```

legacy 排队任务可通过 `Future.cancel` 取消；legacy 已运行任务返回
`unsupported_running_legacy`，不会伪报停止。重启后只恢复脱敏元数据；原先
在运行的 job 变为 `unknown`，不会凭旧 PID 认领进程或自动重跑。

### CNB/SSH 远端合同

`agy_remote_start` 只接受开发目标。`remote_context` 必须包含
`workspace_id/repository/ssh_alias/worktree/branch/baseline_sha`、
`target_kind="development"`、允许/禁止路径、`evidence_refs`，以及调用方
独立回读的 `identity`：

```json
{
  "workspace_id": "cnb-workspace",
  "repository": "nexthorizon/project",
  "ssh_alias": "cnb-dev",
  "worktree": "/workspace/.worktrees/issue-44",
  "branch": "codex/issue-44",
  "baseline_sha": "0123456789abcdef0123456789abcdef01234567",
  "target_kind": "development",
  "owned_paths": ["apps/server"],
  "forbidden_paths": ["production", ".git"],
  "evidence_refs": ["artifact://cnb-workspace/identity"],
  "identity": {
    "workspace_id": "cnb-workspace",
    "repository": "nexthorizon/project",
    "worktree": "/workspace/.worktrees/issue-44",
    "branch": "codex/issue-44",
    "baseline_sha": "0123456789abcdef0123456789abcdef01234567",
    "observed_at": "2026-11-06T10:00:00+08:00",
    "source": "caller_verified"
  }
}
```

身份回读必须在 5 分钟内，且字段与声明完全一致。`worktree` 是 Linux POSIX
路径，只用于调用方通过已授权 SSH 命令工作；桥的本地 AGY 进程 cwd 始终是
独立的 `coordinator_dir`，不会把 `/workspace/...` 传给 Windows。

桥不保存 SSH 私钥、密码或令牌，不自动关闭或替换 CNB 工作区。开工会绑定
有效 `preflight_id`、能力摘要、策略摘要和 `execution_options`。状态报告
始终分成：

- `local_job_status`：本地 managed 进程状态；
- `remote_termination=unknown`：桥不能证明远端进程已停止；
- `business_acceptance=not_started`：业务改动和测试尚未验收。

要求自动远端停止、清理或接管证明的合同返回
`unsupported_remote_lifecycle`。真实 CNB 验收仍须在具体业务任务恢复后批准
目标、身份和命令，不能以本机 fixture 代替。

## 5. 本地验证与开源文件范围

安装测试和构建依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\mcp-antigravity-bridge[dev,winpty]" build
.\.venv\Scripts\python.exe -m pytest -q tests mcp-antigravity-bridge/tests
.\.venv\Scripts\python.exe scripts/validate_skill.py
.\.venv\Scripts\python.exe -m compileall -q mcp-antigravity-bridge/src
.\.venv\Scripts\python.exe -m build mcp-antigravity-bridge
git diff --check
```

真实 MCP 回归测试会创建临时 Git 仓库，再调用协作 dry-run，确认能返回计划且没有创建工作区。该测试不需要 AGY 登录。

适合提交到公开仓库的内容是源码、回归测试、安装说明、配置模板和技能文档。个人完整 `config.toml`、登录凭据、代理凭据、配置备份、日志、虚拟环境和本机安装记录留在本地。`.gitignore` 不会自动清除已提交的敏感信息，公开前还应检查提交内容与历史。

## 本派生版本的兼容修复

原版本在 Windows MCP stdio 环境调用 Git 时可能继承 MCP 输入管道，导致协作预检无法及时返回。本版本为 `GitWorktreeManager` 的两个 Git 调用设置独立的空标准输入，并增加真实 MCP 回归测试；原有工具名称、任务范围检查和人工合并流程保持一致。


## 后台调用的权限与输出问题

### `request-review` 下只读命令被拒绝

`agy -p` 使用后台模式，没有交互式审批界面。即使命令只读，只要没有匹配的许可，也可能返回 `user denied permission to run command`。AGY 的顶层 `SUCCESS` 或退出码 `0` 不能单独证明任务完成，必须检查 `denied_actions` 和实际 `response`。

旧的按项目常驻许可仍可写入
`%USERPROFILE%\.gemini\antigravity-cli\settings.json`，但它会影响其他
AGY 会话。新任务应优先完成 `agy_policy_probe`，再通过
`execution_options` 传递精确命令；以下全局配置仅作为兼容路径。例如，只
授权仓库状态和当前提交查询：

```json
{
  "permissions": {
    "allow": [
      "command(git status --short)",
      "command(git rev-parse HEAD)",
      "command(git status --short; git rev-parse HEAD)"
    ]
  }
}
```

保留原有配置，修改前备份。全局许可会影响其他 AGY 项目；`deny` 和 `ask` 的优先级高于 `allow`。普通命令规则默认按 token 前缀匹配；Windows 无法拆分组合命令时会要求完整匹配。示例的第三条用于此类场景，内容仍然只有同样两个命令。不得扩大为所有 Git 命令。

重试时可向 Codex 发送：

> 请通过 agy 在当前项目仅执行 `git status --short; git rev-parse HEAD`，报告实际输出。保持 request-review，不启用权限绕过；不修改文件。若权限被拒绝、回复为空或任务未完成，请报告失败并停止。

完整开发任务还需要授权相应的测试、构建等命令；上述两条规则只用于状态和提交检查。后台无法批准的操作可以改为由用户在 AGY 交互界面审批，或先授权具体命令再重试。

本版本将权限拒绝报告为 `AGY_PERMISSION_DENIED`，将空回复报告为
`AGY_EMPTY_OUTPUT`；异步和协作结果保留 `reason=permission_blocked`、
`conversation_id`、可获得的 `failed_command`、`permission_resource`、
`retryable=false` 及脱敏 JSON。检测到权限拒绝后不会通过 PTY 再次执行同一
个请求。

### 中文回复触发 `NoneType` 错误

AGY 输出使用 UTF-8，而部分 Windows Python 环境默认使用 GBK。旧版本读取中文时可能先发生 `UnicodeDecodeError`，随后出现 `expected string or bytes-like object, got 'NoneType'`。

本版本显式使用 UTF-8 读取 AGY 的标准输出和错误输出，异常字节采用替换处理，防止整段输出丢失。更新源码或重装包后，重启正在运行的 Codex/MCP 服务，让服务加载新代码。

官方说明：[后台权限行为](https://antigravity.google/docs/cli/headless#permissions-in-headless-mode)、[CLI 权限配置](https://antigravity.google/docs/permissions?tab=cli)。
