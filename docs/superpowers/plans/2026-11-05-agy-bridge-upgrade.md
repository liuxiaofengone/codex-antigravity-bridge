# AGY 桥能力、权限与生命周期改造实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 按本机 CLI 的实际能力提前发现命令许可缺口，支持经验证的任务级设置，并给出可追溯、可取消的执行结果。

**架构：** 保留现有 Python/MCP 入口，分离能力检测、许可构造、结果解析和进程生命周期。首批交付诊断与任务许可；第二、三批分别交付本地作业管理和云端任务契约。

**技术栈：** 现有 Python >=3.10、MCP 1.x、pytest；本机验收使用 `.venv/native-arm64/Scripts/python.exe`（当前 Python 3.14.10 ARM64）。不为规划安装新依赖。

**规格：** [AGY 桥能力与权限改造设计](../specs/2026-11-05-agy-bridge-capabilities-design.md)。

## 全局约束

- 状态：三批（任务 1–6）已于 2026-11-06 实现并通过本机 fixture 验收；不恢复 Issue #43，真实 CNB/SSH 业务验收仍待具体任务批准。
- 本机 CLI 已观察为 1.5.1；参数支持按 help 发现，行为支持按隔离实验确认，不只比较版本号。
- 不自动改全局 settings；默认继承现状、不打开绕过许可、不默认开启 auto。
- 六个旧工具名称、旧调用默认行为及成功返回约定保留；新增字段以可选方式演进。
- 新工具在发布前不写进项目规则作为已存在的强制调用；仓库技能源与包内副本同时更新。
- 无业务项目改动；不调用真实 CNB、SSH 或生产系统完成本计划的自动测试。
- 每任务先写失败测试，再最小实现，再运行对应测试；每批完整回归。提交和推送按届时用户授权执行。

## 审查重点

1. CLI 升级后旧能力缓存必须失效；unknown 不能变成 ready。
2. 会话许可不得丢失 deny/ask、污染其他任务或偷偷落入全局设置。
3. 权限拒绝即使 exit=0/SUCCESS 也不能通过，且不能触发隐式重复执行。
4. 取消与开始同时发生时，不能让取消后的作业继续启动，也不能误杀其他任务。
5. 本地/远端目录、进程和完成状态不能相互替代；重启和断连不得自动重跑。

## 第一批实施结果

- 本机 `agy 1.5.1` 的 help、版本、Python ARM64 运行时和设置摘要可由 `agy_doctor` 只读回读；模式可用性保持 `unknown`。
- 真实本机 canary 在临时 Git 仓库中验证了 authorized、unapproved 和 explicit deny 三种会话；A 的 `state=DONE` 工具输出包含唯一标记，B 被拒绝，C 命中 deny 规则，设置文件字节未变化。私有证据有效 24 小时。
- 真实 `agy_ask(execution_options=...)` 成功执行唯一任务级 echo；默认 argv 和六个旧工具名称保持不变。
- 真实主动 `agy_preflight(active_probe=true)` 只提交一次 `git status --short` 检查，返回 `state=ready`、`evidence_grade=cli_tool_result`，报告 5 分钟失效。
- 全量回归 `176 passed, 1 skipped`；skip 是不带显式 opt-in 的集成 canary，真实 canary 已在上文单独执行并记录证据，不把 skip 写成通过。
- ARM64 环境构建 `codex_agy_bridge-0.2.0` wheel/sdist；独立安装环境完成模块、技能资源、MCP stdio 握手、九工具枚举、doctor 和静态 preflight 验证。

## 第二、三批实施结果

- managed 使用一次性 `stream-json` 与本地受控进程；legacy 仍是 `agy_start` 和 `agy_collab_start` 的默认路径。
- Windows 优先尝试 Job Object。当前宿主对嵌套 Job Object 有限制时会尝试仅针对该 job PID 后代的有界进程树终止，不按 `agy/python/ssh` 名称清理；证据如实保留 `job_object_assigned` 和 `termination_confirmed`。
- PowerShell 子进程、超时、运行时取消、其他 job 不受影响、持久化重启恢复、legacy 排队/运行取消边界均有真实本地 fixture 覆盖。
- `agy_cancel` 区分 legacy 排队取消、运行中 `unsupported_running_legacy`、managed 本地进程树取消和永远 `unknown` 的远端状态。
- `agy_remote_start` 将 Linux 远端工作树与 Windows 本地 cwd 分离，绑定 5 分钟预检、策略、调用方独立身份回读和 evidence refs；不提供远端停止/清理确认。
- 全量本机结果：`204 passed, 1 skipped`。skip 仍是不带显式 opt-in 的模型 canary；新的 PowerShell canary 已单独真实执行并五项全部通过。
- 版本升级为 `0.3.0`；未调用 CNB、SSH 或生产系统。

## 文件职责

新增文件均在 `mcp-antigravity-bridge/src/codex_agy_bridge/` 下：

| 文件 | 职责 | 批次 |
| --- | --- | --- |
| `agy_capabilities.py` | CLI 定位、help 能力、快照及失效 | 1 |
| `agy_policy.py` | 执行选项、会话许可与授权引用校验 | 1 |
| `agy_evidence.py` | 本机许可 canary 证据保存、覆盖范围与失效 | 1 |
| `agy_result.py` | CLI 结果解析、脱敏、兼容错误详情 | 1 |
| `agy_preflight.py` | 合同验证、静态与主动预检、证据失效 | 1 |
| `agy_process.py` | 作业拥有的进程、取消和超时 | 2 |
| `agy_job_store.py` | 用户私有作业元数据及重启恢复 | 2 |
| `agy_remote.py` | 开发远端合同与目标证据，不等同原生 remote-agent | 3 |

现有 `agy_runner.py` 负责编排，`server.py` 保留适配层；`agy_jobs.py` 和 `agy_collaboration.py` 传播结果与进程状态。按任务新增对应 `tests/test_*.py`，不将所有逻辑堆进 server。

## 第一批：先解决此次许可缺口与诊断困难

### 任务 1：本机能力发现和安全 canary 门槛

**文件：** 新增 `agy_capabilities.py`、`tests/test_agy_capabilities.py`；修改 `server.py` 与 `test_mcp_stdio.py` 中受新增工具影响的枚举断言。

**接口：** `inspect_agy_capabilities(refresh: bool = False) -> CapabilitySnapshot`；新增 MCP `agy_doctor(refresh=False)`。快照字段固定为 `capability_id/binary_path/cli_version/python_platform/declared_flags/mode_availability/settings_summary`。

- [x] 写测试：help 含 `--settings` 只得到 declared=true；没有 canary 证据时 availability 仍为 unknown；二进制 mtime/大小或设置摘要变化使 capability_id 变化。
- [x] 写测试：无二进制、help 失败或格式未知返回明确错误，日志与输出不包含完整配置/凭据；doctor 不运行任何 `-p` 命令。
- [x] 运行 `python -m pytest -q mcp-antigravity-bridge/tests/test_agy_capabilities.py`，先确认失败。
- [x] 实现上述接口及工具。缓存仅是诊断优化，不作为永久许可证明。
- [x] 通过测试后记录本机 1.5.1 help 快照；此时不下发模型 prompt。
- [x] 在本任务独立检查通过后提交，按届时用户授权处理推送。

### 任务 2：保留具体阻断与会话证据

**文件：** 新增 `agy_result.py`、`tests/test_agy_result_details.py`；修改 `agy_runner.py`、`agy_jobs.py`、`agy_collaboration.py`、`server.py`；扩展 `test_agy_result_validation.py`、`test_mcp_stdio.py`。

**接口：** `parse_agy_result(stdout: str, stderr: str, exit_code: int, *, output_format: str | None) -> AgyResult`；`failure_details(result: AgyResult) -> dict`。结果字段与兼容策略按规格第三节执行，旧 runner 中相关导出保留兼容入口。

- [x] 先写 fixture：JSON 中 `SUCCESS/response空/denied_actions非空`，stderr 同时有通用提示。断言保留 conversation_id、可获得的 failed_command、permission_resource，error_kind=permission，reason=permission_blocked，retryable=false。
- [x] 写缺失证据、stderr 噪声、ANSI、中文、业务 JSON 回复、取消/未完成 CLI 状态测试；缺少具体命令必须为 null，不能猜测 `cnb status`。
- [x] 先红后绿，实现 parser；同步错误保持旧前缀并附脱敏 JSON，异步/协作保留结构化字段。
- [x] 运行 `python -m pytest -q mcp-antigravity-bridge/tests/test_agy_result_validation.py mcp-antigravity-bridge/tests/test_agy_result_details.py mcp-antigravity-bridge/tests/test_mcp_stdio.py`。
- [x] 真实 MCP fixture 断言同步 isError=true、状态查询中的任务 failed；不能把查询本身正常返回当作任务成功。通过后提交本任务。

### 任务 3：验证并接入仅当前任务生效的许可

**文件：** 新增 `agy_policy.py`、`agy_evidence.py`、`tests/test_agy_policy.py`、`tests/test_agy_policy_evidence.py`、`tests/test_agy_session_settings_integration.py`；修改 `agy_runner.py`、`server.py`、`agy_jobs.py`、`agy_collaboration.py`。

**接口：** `ExecutionOptions(mode='inherit', approved_commands=(), authorization_ref=None)`；`build_session_args(options, capabilities, inherited_permissions) -> list[str]`。四个执行工具追加 `execution_options: dict | None = None`，统一校验后再传给 runner，不破坏现有位置参数含义。新增 `agy_policy_probe(authorization_ref, timeout=60)`，doctor 仅回读证据。

- [x] 先写参数构造测试：缺授权引用、旧 CLI 不支持、未知模式、许可与 skip=true 冲突均失败；默认参数保持旧 argv；不把具体命令泛化为宽前缀规则。
- [x] 在实施阶段经批准的本地临时仓库内，用唯一的无副作用 fixture 命令做能力 canary。对比会话 A/B 的许可隔离、已有 deny/ask 优先级及全局文件前后字节；记录真实 flags、退出码和结果字段。
- [x] canary 必须设置独立超时并包含读取/写入边界，不调用 CNB、SSH 或真实业务命令。计入 AGY 调用次数；证据不足即停止该能力接入，保持 policy_unverified，不扩大权限求通过。
- [x] 实现 `PolicyEvidence` 与私有证据存储；测试 CLI/基础策略/平台/builder_version/模式或选择器覆盖不匹配、24 小时到期、记录缺失/损坏、必需检查未执行时全部 policy_unverified。运行时每次构造许可都核验，不把集成测试通过视为永久开关。新探针工具加入时同步更新受影响的工具枚举测试。
- [x] 根据实测的嵌套合并语义实现唯一确定的构造方式；不能可靠保留限制时阻断而非猜测。测试会话退出/失败/取消都不持久化许可。
- [x] 运行 `python -m pytest -q mcp-antigravity-bridge/tests/test_agy_policy.py mcp-antigravity-bridge/tests/test_agy_policy_evidence.py`。集成测试默认 skip，只有显式 opt-in 和对应授权才运行；跳过不写成“已通过”。
- [x] 四个执行入口（含协作入口）统一传递 options，补充每条路径测试；通过后提交本任务。

### 任务 4：预检与第一批调用契约发布

**文件：** 新增 `agy_preflight.py`、`tests/test_agy_preflight.py`；修改 `server.py`、`test_mcp_stdio.py`、`docs/windows-setup.zh-CN.md`、`skills/agy-supervisor/SKILL.md` 与其 `references/agy-supervisor-protocol.md`；同步包内 `resources/agy-supervisor/` 对应副本；修改 `pyproject.toml` 将首批功能版本设为 `0.2.0`，由 doctor 回读实际安装版本。

**接口：** `preflight(contract: TaskContract, active_probe: bool = False) -> PreflightReport`；新增 MCP `agy_preflight(contract, active_probe=False)`。报告字段为 `preflight_id/contract_digest/capability_id/policy_digest/checks/state/expires_at/blockers/enforcement/evidence_grade`；枚举与 5 分钟有效期按规格。

- [x] 先写静态检查测试：无主动执行时只能 ready_for_probe/blocked/unknown；不创建模型调用、远端工作区或新代理。
- [x] 写命令缺授权、策略不透明、plan 模式却请求执行、CLI/配置变化、超过 5 分钟、远端身份缺失和 cwd 混用用例；不得依赖自行模拟的完整 CLI 权限引擎。
- [x] 写主动检查 fixture：桥只提交一次请求，观察到拒绝后 blocked 且不启动后续任务；要求硬性工具拦截的合同返回 unsupported_enforcement。模型自报、意图回复、缺失输出或目标不符不是 ready。
- [x] 实现验证、摘要与主动调用；ready 只接受本机已验证格式的 CLI 工具结果或调用方独立核验结果；CLI 无可验证执行事件时必须 unknown。ready 证据不替代实际开工的身份回读。第一批先不以预检令牌强行改变旧六工具的默认行为，调用方规则负责阶段门禁。
- [x] 更新文档，标清 doctor/preflight 为本批新增工具；同步测试原六工具仍存在，新增 doctor、policy_probe、preflight 三工具可用，而非继续要求工具总数恰好为六。
- [x] 第一批完整运行 `python -m pytest -q tests mcp-antigravity-bridge/tests`、`python scripts/validate_skill.py`、`python -m compileall -q mcp-antigravity-bridge/src`、`git diff --check`。记录实际通过/跳过数。用 ARM64 环境构建 wheel/sdist，在独立安装环境验证模块、技能资源与 MCP 握手；发布后重启 MCP 并回读 doctor 的版本与工具，不能以源码已更新代替运行进程已加载。审查后再进入下一批。

**第一批完成标准：** 用 fixture 重现“Git 检查获许可，云命令未获许可”的差异；调用前能识别缺口或明确 unknown，失败后直接拿到可用诊断；新任务许可通过本机 canary 才可宣称可用；默认不修改全局配置。

## 第二批：长任务生命周期

### 任务 5：可取消进程与持久作业证据

**文件：** 新增 `agy_process.py`、`agy_job_store.py`、`tests/test_agy_process.py`、`tests/test_agy_job_store.py`；修改 runner、jobs、server 与 async/stdio 测试。

**路由：** 旧同步工具和未指定 process_control 的旧异步/协作调用保留 legacy；显式 managed 仅支持 headless。取消 legacy 运行中作业返回 unsupported_running_legacy；排队取消须由 Future.cancel 确认。每条状态暴露取消支持范围。

**接口：** `ManagedAgyProcess` 提供 `run()/request_cancel()/termination_evidence()`；`cancel(job_id: str, wait_timeout: float = 10) -> CancelReport`；新增 MCP `agy_cancel`。`JobStore` 提供 `save(record)/load(job_id)`，目录读取 `AGY_BRIDGE_STATE_DIR`，默认用户私有目录。

- [x] 先写真实本地 fixture 进程测试：legacy/managed 路由、terminal 与 managed 冲突、legacy 已运行取消不伪报终止、排队时取消、启动竞争、运行时取消、超时、持有孙进程、终止失败；其他 fixture job 必须不受影响。未创建 AGY 真实任务。
- [x] 为新 managed 路径使用一次性 JSON/stream-json 输出；保持单 prompt，stdout/stderr 并发读取。不要启用长期 stream-json 输入队列，也不要把关闭 stdin 当作立即取消。
- [x] 新 managed 路径不得因空输出自动 PTY 重跑。若保留旧接口的 legacy PTY 兼容路径，明确其行为，不能声称具备 managed 路径的单次执行保证。
- [x] Windows 实测 Job Object 及其子进程约束；无法可靠接管进程树时报告 termination_confirmed=false。POSIX 用自己创建的进程组。禁止按可执行文件名全局清理。
- [x] 写重启恢复测试：保留已知结果和标识；无法证明存活时 unknown，不凭复用 PID 认领进程、不自动重跑。日志与持久化不存完整 prompt/凭据。
- [x] 对 legacy state 保持兼容，在附加生命周期字段区分取消受理与停止确认；远端任务一律不从本地退出推导已停止。
- [x] 运行对应单元、真实进程与完整 MCP 回归后，才发布新增取消工具；本批不新增“AGY 官方会话归档”接口。

## 第三批：CNB/SSH 任务适配

### 任务 6：本地编排与远端工作树分离

**文件：** 新增 `agy_remote.py`、`tests/test_agy_remote.py`；修改 preflight、jobs、server、技能源/包内副本与文档。

**接口：** 新增 MCP `agy_remote_start(prompt, coordinator_dir, remote_context, preflight_id, execution_options=None, timeout=900)`，固定使用 managed。`remote_context` 固定包含 `workspace_id/repository/ssh_alias/worktree/branch/baseline_sha/owned_paths/forbidden_paths/evidence_refs`；不得传 SSH 私钥或凭据。

- [x] 先写 fixture：Linux 远端路径绝不作为 Windows cwd；本地路径存在不代表远端可用；非开发目标、失效预检、身份变化、许可变化均阻断。
- [x] 写首版边界 fixture：remote_termination 始终 unknown；本地完成、SSH 断连、调用方提供 evidence_refs 都不能将其升为 confirmed。要求自动远端停止/接管证明的合同返回 unsupported_remote_lifecycle；不能自动关闭或替换工作区。
- [x] 实现专用工具，保留旧 agy_start 的本地隔离约定。远端 ready 后开工仍重新核对 workspace/SHA；不将 `--remote-agent` 映射为 CNB SSH。
- [x] 真实 CNB 验收仅在用户恢复业务任务且批准具体预检命令后执行；本机 fixture 通过不能代替云端验收。本批只保存独立证据引用，不验证远端退出；真正的停止确认需另写远端任务 ID/进程归属/退出码/独立查询协议规格，获审阅后另立实施计划。
- [x] 回归通过后明确报告本地作业状态、远端证据和业务验收三个不同层次，再按授权提交发布。

## 发布顺序与交接

先独立审阅、实施并验收任务 1—4；任务 5、6 是后续批次，实施前再次核对 CLI 帮助和本机能力。首批验证结果可能改变后续进程设计，不能并行提前实现。

本次仅准备上述文档，不创建业务工作树、不修改全局许可、不运行新的 AGY 任务、不自动提交或推送。项目 AGENTS.md 由项目维护者合并规格中的规则建议，并沿其既有文档同步流程处理。
