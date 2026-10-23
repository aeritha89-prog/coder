# 评测接入与统计口径

## 1. 成绩对应的代码与产物

原始评测副本的 Git 基线为 `c2c009c8591baba8489b64af26359d711fb39a14`，使用改名前的 `study_coder` 包和 `STUDY_CODER_*` 配置。

该副本不是干净检出：新增 `evals/tb/` 适配层及对应测试，修改了 `pyproject.toml`，业务源码中还有本地记忆日志措辞改动。因此，基线提交号不能单独代表完整评测实现。

当前项目整理前的提交为 `82b3bd478ba4ac1e1205fa1d5f79cc29262f7f15`，已改名为 `coder`。本次评测使用上述副本中的 Harbor 适配层，其运行方式与项目默认 CLI 的差异见第 3 节。

本目录的 `adapter/` 提供适用于当前源码的适配文件：导入包名和配置前缀统一为 `coder` / `CODER_*`，仓库根目录定位适配当前目录层级，入口为 `evals.tb.agent:CoderAgent`，轨迹文件名为 `coder-session.json`。Worker 传输、工作目录映射和服务保留逻辑沿用评测实现；指标 Markdown 统一展示全部任务的通过率。原评测记录中的旧名称及原始文件指纹保持原样。

证据目录（相对于评测副本根目录）：

```text
.cache/harbor-jobs/tb-final/
  FINAL_REPORT.md
  metrics.json
  metrics.md
  trials/                       89 个任务的评测记录
  supporting/                   评测辅助记录
```

`data/provenance.json` 保存原报告、指标、89 份 result/config/lock、74 份 session、相关 verifier 输出及适配源码等 404 份文件的指纹。源码与 Worker 归档指纹记录的是整理时状态；各任务执行时的不可变构建标识未单独保存。

## 2. Harbor 如何调用 Agent

```text
Harbor 创建任务容器并提供任务指令
  → StudyCoderAgent.setup：上传 worker.pyz、探测 Python 和工作目录
  → StudyCoderAgent.run：在线程中运行同步 Agent 循环
  → HarborRuntime：把工具调用转发回 Harbor event loop
  → environment.exec：在该任务容器里执行工具
  → Agent 返回结果，写入用量与 session
  → Harbor verifier 检查任务成果并写 reward
```

模型请求由宿主发出。适配器通过 `asyncio.to_thread()` 执行同步 Agent，通过 `asyncio.run_coroutine_threadsafe()` 将工具请求交回 Harbor 的异步接口。

任务容器、任务环境和 verifier 的生命周期由 Harbor 管理；本项目原生的 `DockerRuntime.start()`、文件导出与宿主应用流程不是这一接入方式的执行入口。

### 适配层文件

适配文件位于本目录的 `adapter/evals/tb/`：

| 文件 | 职责 |
| --- | --- |
| `evals/tb/agent.py` | external agent 生命周期、工作目录探测、参数装配和 verifier 环境准备 |
| `evals/tb/runtime.py` | `WorkspaceRuntime` 接口、同步与异步桥接、取消和工具请求转发 |
| `evals/tb/build_worker.py` | 打包 Worker 与纯 Python 依赖，设置评测专用 bootstrap |
| `evals/tb/result.py` | 回填 Harbor 用量和 metadata，原子保存 session |
| `evals/tb/metrics.py` | 汇总任务记录、计算工具与验收指标 |

原报告记录适配器单测为 11 passed。本目录的 `adapter/tests/` 包含对应协议测试，以及归档在独立解释器中的启动测试。复现命令见 README 的“复现方法”。

## 3. 与项目默认运行方式的差异

| 项目 | 评测设置或处理 |
| --- | --- |
| 任务环境 | 使用 Harbor 提供的任务 Docker 容器 |
| Agent 轮次 | `max(宿主配置, 2000)`；这是源码下限，不是每个 trial 已记录的实际值 |
| 本地上下文预算 | `max(宿主配置, 200000)`；不表示已修改模型服务端窗口 |
| 默认工具命令超时 | `max(宿主配置, 1800)` 秒；仍可能被 Harbor 的任务级超时提前终止 |
| 工作区逻辑预算 | 64 GiB、1,000,000 个文件 |
| 权限 | `DefaultPermissionPolicy(yes=True)` |
| 长期记忆 | 关闭 |
| 技能 | 空目录配置，关闭加载 |
| 后台工具与服务工具 | `background_enabled=False`，`service_ports=()` |
| 导出预检 | 工具请求设置 `STUDY_CODER_EXPORT_EXCLUDE='["*"]'`，跳过中途交付检查；最终成果由 verifier 评判 |
| 工作目录 | 探测真实目录，必要时创建 `/workspace` 链接；74 个有记录 trial 中，71 个在 `/app`，其余各 1 个在 `/app/personal-site`、`/workspace`、`/app/dclm` |
| 无 Python 的镜像 | bash-only 降级；文件及任务工具可返回不可用错误 |
| 常驻进程 | `STUDY_CODER_KEEP_BACKGROUND=1` 时跳过每条命令结束后的后代清理，保留服务到 trial 结束 |

74 个有 session 的 trial 中，setup 的 Python 记录分别为 3.13：35 个，3.12：12 个，3.11：3 个，3.10：2 个，未找到可用解释器：22 个。后者与另外 15 个未保存 session 的超时 trial 是不同口径。

zipapp 仅打包 `study_coder`、`dotenv`、`yaml` 等 Worker 所需代码，去掉宿主平台二进制依赖，并替换工具包初始化入口，避免容器额外加载 Tavily。请求先上传为文件，再重定向给 Worker，以避开 Windows 长命令行限制；Worker 响应使用哨兵标记处理合并的 stdout/stderr。

### 任务级超时与环境改动

89 份 lock 的 `timeout_multiplier` 均为 1.0，环境均为 Docker，verifier 均为 shared 模式。原报告说明未修改任务 `task.toml` 的超时与资源定义；本次核对了 lock，没有把报告中的声明扩展为对历史任务目录全部文件的独立校验。

适配器确实对部分 verifier 环境做了准备：

- 对 `merge-diff-arc-agi-task`、`qemu-startup`、`write-compressor` 上传静态 Linux `uv/uvx`。
- 对 `qemu-startup` 修正 bullseye 软件源并准备依赖。
- 原运行记录说明，`reshard-c4-data` 使用了大镜像预缓存。

## 4. 评测范围与数据来源

本次评测覆盖 Terminal-Bench 2.1 的全部 89 道任务。本目录归档 `tb-final/metrics.json` 对应的 89 条任务记录，并逐一核对同目录的原始文件。

每道任务对应一条评测结果。逐题数据包括验收结果、异常标记、Agent 执行时间，以及可用的模型用量和工具调用记录。

89 道任务的来源字段均为 `terminal-bench/terminal-bench-2-1`。每题任务内容引用 `sha256:...` 保存在 `data/tasks.csv` 的 `task_ref` 列，来源 job 标识保存在 `source_job_id` 列，便于定位原始记录。

所有统计以任务的 result、session 和 verifier 输出为依据，辅助记录用于补充环境和异常背景。

## 5. 指标定义

### 主通过率

```text
resolved = verifier_result.rewards.reward == 1.0
通过率 = 通过任务数 / 全部任务数 = 75 / 89
```

`torch-pipeline-parallelism` 没有 `verifier_result`，仍属于本次评测的 89 道任务，计入未通过；其 reward 保留为 null。

### 错误分类

本次评测通过 75 道，未通过 14 道。未通过任务中，10 道属于超时分类，4 道正常结束但验收失败。通过率统一使用全部 89 道任务作为分母。

### Token、工具调用与恢复

- Token 取自 Harbor `agent_result` 中的模型用量，只有 74 条非空记录。输入 token 包含多轮模型请求的累计用量，不是对每段历史文本去重后的字数。
- 工具调用取自 session 的 `tool_results`，`is_error=true` 为失败。没有 session 的 15 个 trial 无法统计调用；CSV 用空值表示，JSON 保留原指标的 0 并额外标注可用性。
- Error Recovery 的定义是：某次失败调用之后，同一轨迹中存在成功调用。核对脚本使用反向扫描独立重算这一数值，没有把“以后成功调用过工具”改写为“任务修复成功”。
- 73 个 trial 出现过工具失败，另外 1 个有轨迹 trial 没有工具失败；因此 73/73 与 74 份轨迹不矛盾。
- 文件工具错误原因按错误文本归类，用于解释实际拒绝和降级情况，不作为更细的因果结论。

### 执行时间与费用

执行时间取每条 result 中 Agent 阶段结束减开始的时间，覆盖 89 条记录。它不包含环境准备与 verifier 阶段；各 trial 的时间合计表示累计 Agent 执行时间，不等于并发评测的实际墙钟耗时。

费用字段全为空，保留 null。缺少计费字段和底层模型版本时，不按网上价格估算本次账单。

## 6. 数据文件与核验范围

`data/results.json` 是规范化后的主数据：

- `metadata`：数据集、Agent 标识、源码基线、报告记录的环境和统计范围。
- `summary`：由 89 条评测记录独立重算的摘要。
- `source_summary`：原 `metrics.json` 的摘要，用于逐字段对照。
- `coverage`：缺失轨迹、可用 token、reward、模型字段、重叠标记和文件工具失败原因。
- `trials`：原 89 条指标记录，并补充任务内容引用、来源 job 和已记录模型等字段。

`tasks.csv` 时间戳沿用原始 UTC，README 的时间范围转换为北京时间。空的 reward、token、轮次，以及无轨迹时未知的工具次数均写为空字段；布尔值使用 `true` / `false`。

仅运行 `verify_results.py` 时，核对本目录的 JSON 汇总、任务唯一性和 CSV。加上 `--source` 时，进一步核对 404 份源文件的指纹，并从原始 result 和 session 独立检查 reward、用时、token、工具调用及失败后的成功调用。脚本只读，不启动模型、Harbor、Docker 或新的任务。

模型日志只记录了端点别名 `deepseek-flash`；没有据此推断供应方实际后端、底层模型修订号或未保存的 15 条运行配置。本目录归档结果、统计口径与证据指纹，完整运行环境未作冻结归档。
