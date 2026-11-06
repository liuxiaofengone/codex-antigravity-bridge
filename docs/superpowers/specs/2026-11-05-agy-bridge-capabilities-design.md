# AGY 桥能力与权限改造设计

状态：第一批已于 2026-11-06 实现并通过本机验收；第二批在独立审阅后重新核对本机能力。

## 目标与责任划分

项目 `AGENTS.md` 负责规定何时委派、谁能授权、开发与交付边界、验收及切换执行者的条件。桥负责兑现可观测的执行能力：识别实际 CLI、传递任务级设置、报告权限阻断、管理本地作业、区分远端未知状态。提示词里的“已授权”不等同于 CLI 已放行，也不是操作系统隔离。

首批目标是让调用方在开始业务任务前发现 CNB 命令的许可缺口，并让诊断包含确切失败证据。默认不开启权限绕过，不自动修改全局设置，不替换 CNB 工作区，不接管支付、生产或 UI 自动化。

## 2026-11-05 本机实测事实

| 项目 | 观察 | 对设计的约束 |
| --- | --- | --- |
| AGY CLI | 2026-11-06 再次读取本机 `agy.exe --version` 为 `1.5.1` | 不能沿用安装时 1.3.3 或设计初稿 1.5.0 的能力假设 |
| Python / SDK | 当前独立环境为 Python `3.14.10`、`win-arm64`、MCP `1.30.0` | 保持本机原生运行时，不恢复 x64 路径 |
| CLI `--settings` | 帮助明确为仅当前会话生效，可传内联 JSON 或文件路径 | 优先研究任务级设置，避免靠新增全局许可解决每个项目的问题 |
| CLI 模式 | 帮助列出 `accept-edits`、`auto`、`plan` | 参数存在不证明账号和沙箱可使用；不默认切换 `auto` |
| 其他参数 | 存在 `--conversation`、`--print-timeout`、JSON/stream-json、`--remote-agent` | 仅能证明参数可识别，不代表桥已支持恢复、取消或 CNB 执行 |
| 桥 | 当前版本 `0.1.0`；六个现有工具 | 保留现有工具名称和默认返回约定 |
| 失败证据 | 已授权少量 Git 查询；`cnb status` 的非沙箱执行被拒 | 优先解决能力预检、任务级许可和诊断信息 |

原始本机命令：`agy.exe --version`、`agy.exe --help`；未向 AGY 下发新 prompt。对应故障会话已只读核对，业务项目保持暂停。

以下均未实测，必须作为实现前置门槛：`--settings` 对嵌套对象/数组的替换或合并规则；已有 deny/ask 的优先级能否完整保留；会话设置是否真的不落入全局文件；Windows 沙箱及账号对 `auto`/`unsandboxed` 的支持；跨进程树取消与远端退出证明。不得用网页描述代替这些本机验证。

## 路线选择

采用“兼容现有 Python/MCP 桥，增量接入本机 CLI 已声明能力”的路线。首批保持 CLI 进程调用，不重写为 AGY SDK/ACP，不启动长期多轮 stream-json 输入会话。后者需要额外的队列、交互和恢复协议，无法直接解决本次许可缺口。

`--remote-agent` 指 AGY 自身的远端代理能力，不能直接当作 CNB SSH 工作区支持。已有 CNB 模式仍是本地 AGY 通过已批准的 CLI/SSH 操作开发环境。

## 第一批：能力、任务许可、结构化诊断及预检

### 1. 能力快照 `agy_doctor(refresh=false)`（拟新增）

只运行本机 `--version`/`--help`，读取脱敏后的相关设置；不调用模型、不访问 CNB、不修改配置。输出 `capability_id`、CLI 路径/版本、Python 架构、帮助声明的 flags、配置来源及执行模式。对模式和沙箱另报 `availability=unknown|verified|unavailable`，不能把参数存在当作可用。

快照按 CLI 绝对路径、mtime、大小和相关设置摘要失效；原始配置内容、凭据和完整权限规则不写日志。版本未知或格式改变时只报告已观察事实。

另增 `agy_policy_probe(authorization_ref, timeout=60)`，由调用方显式批准后才运行本地 fixture canary，不把模型调用隐藏在 doctor 中。探针按每轮最多三次 AGY 调用执行，缺失或失败检查不能计为通过。

通过的检查保存为 `PolicyEvidence`：`evidence_id/cli_fingerprint/base_policy_digest/platform/builder_version/tested_modes/selector_shapes/checks/verified_at/expires_at`。`checks` 必须逐项记录许可隔离、deny/ask 保留和全局文件不变等实际证据。由桥自己运行探针生成，判据来自经过核验的 CLI 工具事件及桥对本地 fixture 的独立检查；仅有模型返回的“已验证”不作为证据。记录保存在 `AGY_BRIDGE_STATE_DIR` 指定的用户私有目录，默认 `~/.codex-agy-bridge/`，有效期 24 小时。

运行时每次构造任务许可都核对以上摘要、TTL、模式及选择器形态覆盖；任一不匹配、必需检查缺失或证据文件不可用，返回 policy_unverified。doctor 只能回读这些证据，并明确其覆盖范围；不能以 CI 通过或某台开发机的证据启用另一安装环境。

### 2. 任务级许可与执行选项

统一 `ExecutionOptions`：`mode=inherit|plan|accept-edits|auto`（默认 inherit）、`approved_commands: tuple[str, ...]`（默认空）、`authorization_ref: str | None`。

调用方明确授权后，桥才将列出的具体命令转换为当前会话的权限设置。`authorization_ref` 是审计引用，不是桥能自动证明的用户授权或安全令牌。不得提供任意 settings JSON 透传入口；只构造经过验证的权限字段与模式参数。这里“任务级”指新增许可只在当前会话生效，不承诺收窄全局原有 allow 或提供操作系统隔离；检测到配置来源不完整时明确说明，不推断完整有效策略。

先完成隔离 fixture 验证：会话 A 的许可不会影响会话 B；deny/ask 保持；全局设置字节不变；已授权的本地测试命令可运行，未授权命令仍被阻断。不能验证这些语义时，任务级许可返回 `policy_unverified`，不得回退为全局改写或权限绕过。

优先使用内联 `--settings`，内容只含无凭据的规则。规则按已授权命令构造，不自动泛化成 `command(cnb)`、`command(ssh)` 或通配正则。保留继承的限制，不通过删除 deny/ask 来解决冲突；语法无法可靠表达则返回 `manual_policy_required`。会话许可仍不保证文件、命令或远端环境的完整隔离，调用方需要独立验证授权范围。

四个执行工具（agy_ask、agy_ask_json、agy_start、agy_collab_start）仅追加可选 `execution_options` 参数，省略时保持原行为；两个状态查询工具不增加执行选项。默认 `dangerously_skip_permissions=false`；旧参数 true 与非空任务许可同时出现时拒绝为参数冲突。旧 CLI 缺少 `--settings` 时，inherit 仍可走旧路径，但明确请求任务许可时返回 `unsupported_capability`。

### 3. 结构化结果与可操作错误

结果模型保留现有 `text/exit_code/used_pty/stderr`，增加可选的 `conversation_id`、`cli_status`、`denied_actions`、`failed_command`、`permission_resource`、`workdir`、`capability_id`、`retryable`、`remediation`。

现有异步 `state=failed` 与 `error_kind=permission` 保持兼容；新增 `reason=permission_blocked`，在许可条件未改变前 `retryable=false`。stderr 与 JSON 元数据同时保留，不能选择其中一个而丢掉会话 ID。

同步旧工具继续使用 MCP `isError=true` 和原错误前缀，在错误文本中附脱敏 JSON 详情；成功时 `agy_ask` 仍返回文本，`agy_ask_json` 仍返回现有 CLI JSON。状态查询本身正常返回，但调用方必须检查任务 state，不能把查询的传输成功当作任务成功。

首批结构化诊断保证仅覆盖 headless 路径。terminal 显示模式保留现有观看流程，但无法捕获的回复/权限详情必须标为 evidence=unavailable；仅有进程退出证据不等于业务验收。主动预检与许可探针固定使用 headless JSON/stream-json，不自动转为可见终端。旧文本调用若缺少 CLI 元数据，保留 evidence=unavailable；不为补字段猜测会话 ID。

优先从正常 JSON 结果获取 ID 和诊断，不依赖生产读取 AGY 私有 SQLite 格式。若 CLI 未提供具体命令，则输出 null 与 `evidence=unavailable`，不从提示词猜测。

### 4. 预检 `agy_preflight(contract, active_probe=false)`（拟新增）

`TaskContract` 包含任务 ID、capability_id、`local|remote_ssh` 执行目标、本地目录、远端上下文（如适用）、许可命令、授权引用、owned/forbidden paths、预检命令。

静态预检只检查输入一致性、已声明能力与明显策略冲突，输出 `ready_for_probe|blocked|unknown`；不能宣称 AGY 的权限评估已通过。对 CLI 不透明的规则不要另写一个猜测式权限引擎。

主动预检只在明确允许后，通过一次 AGY 调用请求执行合同中列出的已批准检查命令。桥保证不自动再次提交 prompt，并在收到拒绝后阻断后续阶段；“同一次模型调用中立即停止、绝不调用其他工具”在第一批只是提示词约束与原生权限策略，桥没有工具调用拦截能力，不能声称强制保证。需要该强保证的合同返回 unsupported_enforcement，不开始探针。不得以桥的观察结果代替实际隔离。

报告增加 `enforcement=policy_and_prompt` 与 `evidence_grade=cli_tool_result|caller_verified|agent_report_only|unavailable`。只有经本机 canary 验证过格式的 CLI 工具结果或调用方独立核验的结果才可支持 ready；固定文本、模型自报命令输出和元数据缺失只能 unknown。若本机 stream-json 没有可验证的工具执行事件，保留 unknown，转由调用方独立执行已批准的检查。首批不为此读取 AGY 私有数据库或伪造强制拦截。

首版静态发现非空任务许可而没有授权引用时直接阻断；plan 模式与需要实际执行命令的预检合同冲突时阻断。远端 ready 需要实际回读工作区标识、仓库、路径、分支、基线 SHA、Git 状态；声明的路径和 agent 自称成功都不足以证明隔离。

`preflight_id` 绑定合同摘要、CLI 能力快照和许可设置摘要，首版有效期 5 分钟。任务提交前重新核对摘要；目标、命令、版本、设置变化即失效。预检不能消除 TOCTOU，开工时仍需重新回读远端身份；首版不声称它是跨进程执行锁。

## 第二批：可取消、可回读的本地作业

`agy_start` 和 `agy_collab_start` 新增 `process_control=legacy|managed`，默认 legacy，旧调用不变；managed 仅支持 headless。同步旧工具保持 legacy，不承诺能用 job_id 取消。新远端工具固定 managed。状态报告声明 `process_control` 和 `cancellation_supported`，不混淆两条路径的保证。

managed runner 改为受控 Popen，每个 job 拥有自己的取消事件和进程句柄。新增 `agy_cancel(job_id, wait_timeout=10)`；区分取消请求已受理、排队任务已取消、本地进程树确已退出、远端状态未知。窗口等待超时不自动取消；任务硬超时则请求停止自己拥有的进程并保留结果。

Windows 优先验证 Job Object；若宿主限制导致无法证明子进程树退出，明确 `termination_confirmed=false`。禁止按进程名杀全部 Python/SSH/AGY。关闭 stdin 仅表示等待当前 turn 结束的优雅关闭，不是立即取消协议。

legacy 排队作业仅在 Future.cancel 确认尚未启动时报告 cancelled；legacy 已运行作业返回 unsupported_running_legacy、termination_confirmed=false，不扫描或猜测 PID。

作业元数据保存在可配置的用户私有目录（`AGY_BRIDGE_STATE_DIR`），不在公开仓库中；只默认保存脱敏的必要元数据，不保存完整 prompt/原始日志。重启后恢复历史证据，不凭 PID 或旧 SUCCESS 宣称仍在运行/已成功，不自动重跑 unknown 作业。

此批只处理本地作业；远端任务保留 `remote_termination=unknown`。本地终止不能证明 SSH 远端子进程已结束。不新增冒充 AGY 官方会话关闭/归档的接口。

## 第三批：云端任务契约

新增专门的 `agy_remote_start`，区分 `coordinator_dir` 与 `remote_context`（开发工作区标识、SSH alias、POSIX 工作树、基线 SHA、允许路径）。不把远端 Linux 路径传给 Windows cwd，也不为了调用异步工具创建没有作用的本地工作树。

首版只接入上下文与预检绑定，`remote_context` 增加 `evidence_refs`，保存调用方独立核验的记录引用及时间；该引用不被桥当成机器可验证的退出证明。首版 `remote_termination` 始终是 unknown，不提供 unknown→confirmed 的转换；本地 job 完成和业务改动验收是另外的状态。桥不据此自动移交执行权、清理远端或切换冲突任务。

真正的远端停止确认单独设计：必须先约定远端任务 ID、所属进程集合、PID 复用防护、退出码、可独立运行的状态查询和证据有效期，再决定接口与状态转换。该协议不在本计划中虚构实现；若调用方要求自动停止/接管证明，则远端执行返回 unsupported_remote_lifecycle。开发目标已通过预检也不能豁免该要求，不自动停止或替换云工作区。

## 可合并到业务项目 AGENTS.md 的调用方规则建议

以下文本供项目维护者合并；当前桥尚无 doctor/preflight/cancel 工具，新增接口发布前须人工完成同等检查，不调用虚构工具。

```markdown
### AGY 委派前置与结果验收

- 委派前核对本机 AGY 版本、当前 MCP 实际暴露工具和目标任务所需能力；不依据旧版提示词假设工具存在。
- 将任务授权与 CLI 许可分开记录。列明执行目标、具体命令、允许文件、禁止操作和授权依据；不因任务已获批准就自动打开全量权限。
- 能力检查、命令与环境预检、代码实现分阶段验收。固定文本回复和工具枚举通过，仅证明连接正常。
- 远端任务分别记录本地编排目录与 CNB 开发工作区/仓库/工作树/分支/SHA；开工前实际回读身份，禁止静默创建或替换工作区。
- permission_blocked 是许可阻断；授权条件未变时不重复调用，不通过改写命令、更换脚本或工具绕过拒绝。需要更改许可时按既有授权流程处理。
- 同步返回、状态查询成功或 CLI SUCCESS 不能单独证明交付；核验实际输出、权限错误、变更文件和对应验收结果。
- 当前执行者停止状态未确认时，不启动冲突任务；本地进程退出不等于远端进程退出。能力不足时明确记为 unknown/能力缺口，不声称已关闭或归档。
- 延续项目现有的实现、推送、CI、部署和 UI 自动化授权边界；环境检查许可不得扩大成这些操作的许可。用户暂停后不得自动重试或回退到其他执行者。
```

## 本机验收与资料

首批单元与真实 MCP 测试全部使用本地 fixture；需要模型的能力 canary 单独标记，在实施阶段获得对应授权后运行。CNB 验收必须等业务任务恢复，并再次核对目标与授权，不能在本次规划中调用被拒的命令。

官方说明仅作语义参考，本机 help 与隔离验证为实施依据：

- [Headless mode](https://antigravity.google/docs/cli/headless)：后台拒绝、结果格式、关闭 stdin。
- [CLI permissions](https://antigravity.google/docs/permissions?tab=cli)：模式、权限优先级及账户/平台差异。

未验证的新版能力不得进入默认执行路径。第一批通过再进入第二、三批，不一次性重写全部桥。
