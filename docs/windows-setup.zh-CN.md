# Windows 安装与 AGY 委派指南

本指南适用于 liuxiaofengone 维护的派生版本。原项目由 crazyzhang277 开发，采用 Apache-2.0 许可证；来源与本地修改见仓库根目录的 NOTICE。

## 环境要求

- 自行安装的 Python 3.10 或以上版本、Git。
- 支持 MCP 的 Codex Desktop 或 Codex CLI，且 `codex` 可以在终端运行。
- Antigravity CLI，且 `agy` 可以在终端运行。
- 首次真实调用前，使用 `agy` 完成交互式登录。

Windows ARM64 如果在安装加密依赖时遇到缺少 wheel、OpenSSL 或 Rust 编译错误，可以使用 x64 Python 3.12 建立环境。选择自己安装的 Python，不依赖某台电脑的 Codex 缓存目录。

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

## 4. 在聊天中委派

普通开发请求不会自动调用 AGY。明确写出任务范围、允许修改的文件、验收标准，以及是否允许合并。

### 只读检查

> 让 agy 只读检查当前项目的登录流程，列出潜在问题和修改建议。不要修改文件。你收到结果后检查建议是否成立。

### 一个实现任务

> 开启 AGY Supervisor，把用户设置页交给 agy。先明确允许修改的文件，并创建独立 Git 工作区。只修改设置页及其专属组件；公共组件、路由、登录逻辑和接口的改动需要先说明。要求昵称保存功能可用，成功和失败都有提示，相关测试通过。完成后由你审查差异、运行测试，暂不合并。

### 与 Codex 并行协作

> 开启 Codex 与 agy 协同开发。你负责后端接口，agy 负责前端页面。先确定接口契约和互不重叠的文件范围。给 agy 派 1 个任务，不打开实时终端。先运行协作预检，再开始实际工作。完成后汇总改动和测试结果，暂不合并。

`agy_ask` 和 `agy_ask_json` 用于同步请求；`agy_start` 和 `agy_status` 用于调用方提供独立工作区的异步任务；`agy_collab_start` 和 `agy_collab_status` 用于自动创建工作区的协作任务。

`dangerously_skip_permissions` 默认关闭。`ready_for_review` 仅表示进程成功退出，仍需审查改动范围并运行验收测试。

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
