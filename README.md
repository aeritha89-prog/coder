# study_coder

依照用户提供的计划，独立复现 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的第一阶段，并补上第二阶段的上下文管理：有界 Agent Loop、容器工作区工具、权限、验证命令、兼容模型、CLI、会话和长历史的模型视图。所有工作区操作只通过 Docker 后端执行。

隔离执行架构与分阶段验收见 [Sandbox 构建方案](docs/sandbox-plan.md)。离线 Docker 后端已有真实容器验收记录；受控网络、预览服务和恢复清理已加入代码，本轮尚未完成真实 Docker 集成验收。首次使用仍需构建镜像并运行预检。

## 启动前准备（Windows PowerShell）

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/guides/projects/)、[Ollama](https://docs.ollama.com/quickstart) 和运行 Linux 容器的 Docker Desktop。以下命令都在**本项目根目录**执行：

```powershell
Set-Location D:\study\Project\coder
uv --version
ollama --version
docker info
```

打开 Ollama 应用，确认本地服务可访问且已安装默认模型：

```powershell
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

`ollama list` 应包含 `qwen3.5:4b`。若缺少该模型，先运行 `ollama pull qwen3.5:4b`；若 API 请求连接失败，先启动 Ollama 应用。自行管理服务时，可以在**另一个终端**运行 `ollama serve`，让该终端保持运行。模型下载需要网络，推理速度取决于本机硬件。

## 安装项目依赖

```powershell
uv sync --extra dev
uv run study-coder --help
```

`uv sync` 按 `pyproject.toml` 和 `uv.lock` 创建 `.venv` 并安装依赖；后续使用 `uv run` 会从这个环境启动程序。`--help` 能显示选项就说明命令行入口已安装。初次同步依赖可能需要网络。

## 配置本地模型

当前工作区的 `.env` 已配置本地 Ollama。新的项目副本若没有 `.env`，在项目根目录执行：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

用于本地运行的关键配置如下，编辑项目根目录的 `.env` 即可切换到另一已安装模型：

```dotenv
STUDY_CODER_MODEL=qwen3.5:4b
STUDY_CODER_BASE_URL=http://127.0.0.1:11434/v1
STUDY_CODER_API_KEY=
```

本地 Ollama 的 [OpenAI 兼容接口](https://docs.ollama.com/api/openai-compatibility)不需要真实密钥；程序会给 SDK 提供本地占位 key。CLI 读取**启动命令所在目录**的 `.env`，不会读取 `-C` 目标项目里的 `.env`。优先级为 CLI 参数、shell 环境变量、`.env`、程序默认值。远程兼容服务须设置真实的 `STUDY_CODER_API_KEY`，调用也可能产生费用。`.env` 已被 Git 忽略，不要提交真实密钥。

Docker 已是唯一执行后端，无需设置 `STUDY_CODER_EXECUTION_BACKEND`；旧脚本的 `--sandbox docker` 仍可使用。若旧 `.env` 含 `STUDY_CODER_EXECUTION_BACKEND=local`，请删除该行或改为 `docker`，否则启动会明确报错。

## 启动 Agent

首次运行前先按下文的 Docker Sandbox 步骤构建镜像并执行 `sandbox doctor`。`-C` 指定 Agent 的**现有工作区目录**；省略时使用当前目录。Docker 是唯一执行后端，预检失败会终止任务。先运行只读任务，确认模型、接口和容器文件读取都正常：

```powershell
uv run study-coder -C .\examples\live_workspace -p '读取 calculator.py，说明 divide 函数目前的行为。'
```

带 `-p` 是单次任务，完成后退出。未带 `--yes` 时，`edit_file`、`write_file` 和 `bash` 都会被拒绝，拒绝结果会返回给模型。若要尝试自动修复，先复制示例项目，再对副本授权：

```powershell
New-Item -ItemType Directory -Force .tmp | Out-Null
$demoWorkspace = ".tmp\demo-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item -LiteralPath .\examples\live_workspace -Destination $demoWorkspace -Recurse
uv run study-coder -C $demoWorkspace -p '修复 calculator.py 中 b 为 0 时没有抛出 ZeroDivisionError 的问题，然后运行 python -m pytest -q 验证。' --yes
```

`--yes` 预先允许这次运行中的容器文件修改和命令执行。默认在任务完成后打印任务 ID，改动仍保存在待审阅导出中。运行 `uv run study-coder sandbox review <任务ID>` 查看差异，确认后运行 `uv run study-coder sandbox apply <任务ID>`，再查看 `$demoWorkspace\calculator.py`。要处理自己的项目，把 `-C` 后的路径改成该项目目录。

持续对话使用交互模式：

```powershell
uv run study-coder -C .\examples\live_workspace
```

在 `study-coder>` 提示符输入任务。涉及修改或命令时，`y` 允许一次、`a` 允许此会话同类工具、`n` 拒绝。输入 `/help` 查看命令，`/tokens` 查看模型提供的 token 用量，`/clear` 清除当前对话历史，`exit` 退出。`/clear` 不会恢复或删除项目文件。

Agent 每次调用工具前会在终端打印 `[工具调用]`，工具返回后打印 `[工具结果]`，包括工具名、参数、成功或失败状态及结果预览。日志不显示内部调用 ID。权限拒绝和中断也会显示为失败结果。为避免输出过长，参数与结果预览分别截取前 500 和 800 字符，并标明截断量；日志截断不影响写入会话历史的工具结果。日志写入标准错误输出，最终答复仍写入标准输出。

每次任务结束后，会在最终答复之后打印 `[工具统计]` 汇总，包括总调用次数和各个已调用工具的次数；输出仍写入标准错误，不显示调用 ID。失败、权限拒绝、未知工具请求及已开始但被中断的调用都计入；中断后尚未开始的调用不计入。交互模式每次输入任务单独统计，不累计旧任务或恢复会话中的历史；任务失败、中断或达到轮数上限时也会打印已发生的调用，没有调用工具时显示总次数为 0。

例如：

```text
[工具统计] 总调用次数=6
[工具统计] "bash" 次数=1
[工具统计] "edit_file" 次数=2
[工具统计] "glob" 次数=1
[工具统计] "grep" 次数=1
[工具统计] "read_file" 次数=1
```

常用附加选项：

| 选项 | 用途 |
|---|---|
| `--stream` | 边生成边显示模型文本。 |
| `--session NAME` | 在宿主的 Sandbox 状态目录下保存历史；用同一名称和 workspace 启动可继续。名称仅支持字母、数字、`_`、`-`。 |
| `--model NAME`、`--base-url URL` | 临时覆盖 `.env` 中的模型和接口地址。 |
| `--max-rounds N`、`--command-timeout N` | 调整最多模型回合数和 `bash` 命令的默认超时秒数；必须为正数。 |

## Docker Sandbox（默认离线）

若要按源码逐步学习导入、工具调用、导出和恢复流程，请看 [Sandbox 学习与阅读指南](docs/sandbox-learning-guide.md)。

需要 Docker Desktop 使用 Linux 容器；受控网络和 loopback 预览要求 Docker Engine 28 或更新版本。首次在本项目根目录运行：

```powershell
uv run study-coder sandbox build --profile general
uv run study-coder sandbox doctor --profile general
```

`build` 只使用本项目的 `sandbox/images/general.Dockerfile`，构建包含常见开发工具和固定 Worker 的镜像。`doctor` 检查 Docker daemon、Linux 容器模式与镜像 ID。预检失败时任务直接终止。

```powershell
uv run study-coder -C D:\study\test --sandbox docker --sandbox-profile general --yes -p '修改项目并运行测试'
```

任务会把目标目录中允许的文件复制到独立 Docker volume。`.env`、私钥、`.git`、虚拟环境、`.npm` 缓存和常见依赖目录默认不会导入，也不会从容器导出；`.env.example` 模板可以导入和导出，仍可用 `STUDY_CODER_SANDBOX_EXCLUDE` 明确排除。导入和导出的单个文件上限为 16 MiB，工作区总容量与文件数量由策略配置。六个工作区工具（读、搜、编辑、写入、命令）使用同一份容器副本；容器内的 `bash` 可执行普通 Linux 命令以及管道、重定向和连续命令。任务容器默认无网络、以非 root 用户运行，具有只读根文件系统及 CPU、内存、进程数、命令输出和工作区逻辑容量限制。Sandbox 模式默认不提供宿主侧 `web_search`；如显式启用，搜索内容会发送给 Tavily，且此开关不代表容器获得网络。模型接口与 API key 留在宿主。

结束时程序停止任务容器、校验导出文件，并打印任务 ID。默认只保存差异，不改写原项目：

```powershell
uv run study-coder sandbox review <任务ID>
uv run study-coder sandbox apply <任务ID>
```

应用时再次检查原文件是否在任务运行期间被用户修改；冲突会停止应用。`--yes` 只授权 Agent 的副作用工具，不自动应用导出文件。若已明确希望自动应用，可配置 `STUDY_CODER_SANDBOX_APPLY=auto` 或传入 `--sandbox-apply auto`。Sandbox 模式下的 `--session` 历史和任务记录保存在宿主的 `~/.study-coder/sandbox/`，不会复制到任务容器。

预览服务默认关闭。把批准的 TCP 端口写入 `STUDY_CODER_SANDBOX_SERVICE_PORTS=3000,5173` 后，Agent 才会看到 `start_service` 和 `stop_service`。服务端口只绑定宿主 `127.0.0.1`；容器接入内部 Docker 网络，没有外网出口。服务命令须保持前台运行，例如 `npm run dev -- --host 0.0.0.0`；工具会等待端口就绪并返回预览地址和 `service_id`，完成后用 `stop_service` 结束服务。结束整个任务时服务随容器一起停止。

如需在容器内下载依赖，可配置 `STUDY_CODER_SANDBOX_NETWORK=allowlist` 和 `STUDY_CODER_SANDBOX_NETWORK_ALLOWLIST=pypi.org,files.pythonhosted.org,*.npmjs.org`。任务容器只连接 Docker internal 网络，唯一出口是独立代理；代理只接受白名单域名的 HTTP 80 与 HTTPS 443，默认拒绝解析到非公网地址的目标，并限制每任务流量总量。HTTPS 代理检查 CONNECT 主机与 TLS SNI 一致；它不解密 HTTPS，所以白名单域名及其服务端仍能看到任务发出的请求内容。网络白名单与宿主 `web_search` 是两项独立配置。

下载失败时先确认实际请求域名在白名单中：`pip` 默认索引使用 `pypi.org`，下载包文件通常还需要 `files.pythonhosted.org`；npm 默认使用 `registry.npmjs.org`。代理拒绝请求时可在任务运行期间查看其容器日志，日志给出处理阶段和简短原因，不记录代理令牌。修改白名单只对新任务生效；更改代理源码后需重新构建镜像。命令末尾接 `| tail` 时，shell 返回的通常是 `tail` 的退出码，排障时应单独运行安装命令。

部分本机透明代理会把公网域名解析到保留的 `198.18.0.0/15` 虚拟地址，默认公网地址检查会拒绝它。**仅当你信任本机的透明 DNS/网络代理**，并确认该虚拟地址可从代理容器连通时，才设置 `STUDY_CODER_SANDBOX_TRUST_FAKE_IP_DNS=true`。此开关只为已列入域名白名单的目标放行这个地址段；回环、普通内网及链路本地地址仍拒绝。它把该虚拟地址段的实际转发目标交给本机透明代理决定，属于对本机网络代理的显式信任。默认保持 `false`。

应用会先保存事务日志和原文件备份；单个文件替换是原子的，执行中报错时会尝试回滚。若进程在应用中被强制关闭，可运行 `study-coder sandbox recover <任务ID>` 回滚；发现并发改动时会保留现场并报告。任务可用 `study-coder sandbox list` 查看；已导出任务的容器和 volume 会自动回收。导出失败时 volume 会保留，可再次运行 `study-coder sandbox export <任务ID>`；确认要删除未导出数据时才使用 `study-coder sandbox cleanup <任务ID> --discard`。

可在启动目录的 `.env` 中配置下列值；示例为默认策略：

```dotenv
STUDY_CODER_SANDBOX_PROFILE=general
STUDY_CODER_SANDBOX_NETWORK=none
STUDY_CODER_SANDBOX_NETWORK_ALLOWLIST=
STUDY_CODER_SANDBOX_TRUST_FAKE_IP_DNS=false
STUDY_CODER_SANDBOX_NETWORK_MAX_BYTES=536870912
STUDY_CODER_SANDBOX_CPUS=2
STUDY_CODER_SANDBOX_MEMORY_MB=4096
STUDY_CODER_SANDBOX_PIDS_LIMIT=128
STUDY_CODER_SANDBOX_MAX_COMMAND_TIMEOUT_S=1800
STUDY_CODER_SANDBOX_COMMAND_OUTPUT_BYTES=1048576
STUDY_CODER_SANDBOX_WORKSPACE_MAX_BYTES=1073741824
STUDY_CODER_SANDBOX_WORKSPACE_MAX_FILES=50000
STUDY_CODER_SANDBOX_ALLOW_WEB_SEARCH=false
STUDY_CODER_SANDBOX_SERVICE_PORTS=
STUDY_CODER_SANDBOX_APPLY=review
```

也可设置 `STUDY_CODER_SANDBOX_IMAGE`、`STUDY_CODER_SANDBOX_STATE_DIR` 和逗号分隔的 `STUDY_CODER_SANDBOX_EXCLUDE`。普通任务网络为 `none`；配置预览端口或域名代理时，使用带 `isolated` gateway mode 的 Docker internal 网络。工作区容量值由 Worker 周期扫描逻辑文件大小和文件数，超限时终止当前命令或服务；它不是 Docker volume 硬配额，短暂写入和文件系统元数据仍可能超过估算值。网络白名单和 loopback 预览依赖 Docker Engine 的 internal isolated bridge 支持。本机已完成真实 Docker 容器验收；更换 Docker、镜像或电脑后应重新运行 `sandbox doctor` 和集成测试。

## 通用后台任务

后台任务已接入 Docker 沙箱，适合安装依赖、构建、测试和有限时长的数据处理。命令启动后立即返回，Agent 可以继续编辑文件、执行前台命令或启动其他后台任务。

更新源码后先重建镜像，后台 runner 必须来自镜像中的可信代码：

```powershell
uv run study-coder sandbox build
uv run study-coder sandbox doctor
```

在项目启动目录的 `.env` 中配置：

```dotenv
STUDY_CODER_SANDBOX_BACKGROUND_ENABLED=true
STUDY_CODER_SANDBOX_MAX_BACKGROUND_JOBS=4
STUDY_CODER_SANDBOX_MAX_COMMAND_TIMEOUT_S=1800
STUDY_CODER_SANDBOX_COMMAND_OUTPUT_BYTES=1048576
```

后台功能默认启用，并发数默认 4，允许配置为 1–16；达到上限只拒绝新的后台启动，文件编辑、查询和取消仍可用。CPU、内存、进程数和网络白名单沿用整个沙箱的配置，各任务共享这些资源。

正常使用时直接在提示中说明需要后台执行，例如：

```powershell
uv run study-coder -C D:\study\test -p "在后台运行测试，等待期间检查并完善 README，最后读取测试结果并解释" --stream --yes
```

`--yes` 预先授权启动和取消等有副作用工具；交互模式可以按次授权。模型需要显式选择后台工具，`bash` 仍同步运行，不会根据命令中的 install/test 等关键词自动切换。

| 模型工具 | 参数 | 返回内容 |
| --- | --- | --- |
| `start_background` | 必填 `command`，可选 `timeout_s`，默认 60 秒 | 任务编号、启动状态 |
| `get_background` | 可选 `job_id`、`tail_chars`，默认 4000 字符 | 指定任务的状态及输出；省略编号时列出活动任务和未读取结果 |
| `wait_background` | 必填 `job_id`，可选 `tail_chars` | 等待该任务结束，返回退出码和日志；等待期间不反复调用模型 |
| `cancel_background` | 必填 `job_id` | 取消该任务并确认后代进程清理，其他任务继续运行 |

后台 `timeout_s` 默认固定为 60 秒，模型可以选择更长时间，但不能超过 `STUDY_CODER_SANDBOX_MAX_COMMAND_TIMEOUT_S`。`tail_chars` 接受 0–8000，返回内容还受 `STUDY_CODER_MAX_TOOL_OUTPUT_CHARS` 限制；程序在序列化前裁剪，保留有效 JSON、状态和退出码。

任务状态包括 `starting / running / succeeded / failed / timed_out / cancelled / lost`。非零退出、超时、取消及结果丢失均明确返回错误状态；一次成功的状态查询不代表命令成功。stdout/stderr 合并，保留日志开头和滚动末尾，超限后继续排空管道并返回截断标志。

完成结果以三种方式交付：终端显示一次完成通知；下一轮模型请求带有限的状态提示；模型调用查询或等待工具获得退出码和日志。等待用户输入时可以看到通知，模型不会自动开始新回合。流式答复和权限询问期间暂存通知，之后再展示；工具日志不额外打印调用或后台任务编号。

后台原始日志不进入系统提示。结果先归档，再随完整工具调用组保存历史，最后标记为已交付；对话压缩后仍可在当前运行时中查询结果。`/clear` 不停止后台命令。恢复 `--session` 只恢复对话，旧容器中的命令不会自动续跑。

并行时需要协调实际依赖：两个安装命令不要同时修改同一依赖目录；后续命令读取前一任务产物时应先等待；测试运行期间修改了相关源码，应在停止修改后重新验证。程序允许并行操作，不声称提供不可变文件快照。

一次性 `-p` 模式需要在当前回合读取所有后台结果；模型过早答复时返回未完成并以非零状态退出，退出会取消活动命令。交互模式返回输入提示符后保持后台运行；显式退出时统一清理。后台工具等待期间 Ctrl+C 会中断本轮并收尾相应任务；现有前台 Worker 控制故障的容器停止兜底仍可能影响整个沙箱。

日志存入宿主 `<沙箱状态目录>/<沙箱任务编号>/background/`，由完成通知显示具体位置。日志先从容器归档，再停止容器和导出工作区；内部记录不混入项目文件。容器日志总预算 16 MiB，宿主归档每次沙箱生命周期总预算 64 MiB，累计最多 32 条后台任务；这些限制与并发数分别计算。

源码阅读顺序：`tools/background.py` 看工具输入输出，`sandbox/background_runner.py` 看容器进程与控制动作，`sandbox/background.py` 看宿主状态与归档，`sandbox/docker.py` 看控制路由与收尾，最后阅读 `agent.py / context.py / cli.py` 的结果交付。设计与验收标准见 [后台任务方案](docs/background-tasks-plan.md)。

本次实现验收（2026-10-02）：全套 265 项测试通过，Ruff 和 mypy 通过；真实 Docker 及当前配置的真实模型都完成了后台启动、继续编辑、等待并读取结果的链路。具体验收范围见方案第 14 节。

## 长对话的上下文管理

Agent 将完整消息保留在 `history` 中，`--session` 保存的也是这份原始历史。每次请求模型前，`ContextManager` 先构造“已有摘要＋未摘要消息”的工作上下文，再按 50% / 70% / 90% 阈值依次判断原样发送、裁剪工具输出、增量摘要和最终压缩。已有摘要覆盖的原始消息不重新计入工作上下文预算；小量新消息不会因归档历史过长而每轮触发摘要。工作上下文再次达到摘要阈值时，仅处理摘要覆盖边界之后的新消息，并与已有摘要合并。一次 assistant 的所有 tool call 与对应 tool result 始终成组保留，最新用户任务保持原文。摘要失败时保留已有摘要并对新增部分生成规则摘要；若当前任务本身仍装不下，运行会明确失败。详细逻辑见 [增量上下文管理](docs/incremental-context.md)。

发生压缩时，终端的标准错误输出会打印 `[上下文压缩]` 日志，显示工具输出裁剪前后的估算 token、旧消息摘要的开始与完成、摘要缓存复用、规则摘要回退及最终压缩结果。日志只显示阶段和数量，不包含对话正文；模型摘要开始时会先打印提示，便于区分摘要等待与普通模型回复等待。

`.env.example` 中的 `STUDY_CODER_CONTEXT_WINDOW_TOKENS=32000` 控制本程序的**估算预算**，可以按本地模型的有效窗口调整。估算不是精确 tokenizer，也不会修改 Ollama 服务端的上下文设置；若服务端窗口更小，仍可能需要调低此值。摘要会额外调用一次或多次模型，其用量在提供商返回 usage 时计入 `/tokens`。

## 不连接模型的自动化检查

```powershell
uv run pytest -q
uv run ruff check src tests
uv run mypy
```

`src/study_coder/llm.py` 中的 `ScriptedLLM` 仅用于自动化测试：它按顺序返回预设回复，不连接 Ollama。正常启动 CLI 时使用 `OpenAICompatibleModel`。单元测试不需要模型服务；真实容器集成测试需要 Docker 和已构建镜像。

## 启动失败时检查

- **连接不到 Ollama**：确认应用正在运行，`Invoke-RestMethod http://127.0.0.1:11434/api/tags` 能返回结果，`.env` 中的 `STUDY_CODER_BASE_URL` 指向 `http://127.0.0.1:11434/v1`。
- **找不到模型**：用 `ollama list` 核对模型名；必要时执行 `ollama pull qwen3.5:4b`，或把 `.env` 的模型名改为已安装的、支持工具调用的模型。
- **workspace 配置错误**：`-C` 必须指向已经存在的目录，程序不会自动创建它。
- **模型只分析、不修改**：单次任务默认拒绝副作用工具；确认目标目录后再使用 `--yes`，或进入交互模式逐次授权。
- **达到最大回合数**：本次任务尚未被程序判为完成；检查已有文件改动和测试结果，再决定是否调整任务或 `--max-rounds`。
- **远程接口提示缺少 key**：仅本机 `11434` 端口的 Ollama 会自动使用占位 key；远程服务需配置 `STUDY_CODER_API_KEY`。

`bash` 的 `command` 参数必填，`timeout_s` 参数可选。省略 `timeout_s` 时使用 `--command-timeout` 或 `STUDY_CODER_COMMAND_TIMEOUT_S` 配置的默认值（默认 60 秒）；模型可对下载、训练等单条长命令指定更长的秒数，例如 `{"command": "python train.py", "timeout_s": 3600}`。单次指定值须为 1–86400 的整数，超时后仍会终止该命令。

`bash` 在 Linux 任务容器内运行，支持管道、重定向和命令串联；能用的程序取决于受信任镜像中安装的工具。文件内容搜索仍建议使用 `glob`、`grep` 和 `read_file`。文件工具通过解析后的 workspace 路径检查，不支持二进制或非 UTF-8 文件。工具输出、命令时间和模型回合均有上限。

会话文件可能包含完整对话和工具输出，请自行保护。当前尚无 Todo、Plan Mode、undo、MCP、hooks 或子 Agent。Docker 模式已通过本机真实容器验收；它仍依赖 Docker Desktop、Linux 内核和受信任镜像的安全边界。


## 技能加载与会话恢复

可信技能放在项目的 `skills/<目录>/SKILL.md` 中；只读取 `SKILL.md`，不会读取同目录的 README。
frontmatter 必须是 YAML 映射，并包含唯一的 `name` 和非空字符串 `description`。示例：

```markdown
---
name: example
description: 说明这个技能适用的任务
---
这里是完整技能正文。
```

修改或新增技能后运行 `uv run study-coder sandbox build`。构建会把技能复制到镜像的
`/opt/study-coder/skills`，任务期间保持只读。启动从固定 image ID 获取技能清单，模型可见的
名称、描述与 Worker 实际加载的正文来自同一镜像。技能内容变化后，现有任务继续使用其原镜像；
新任务使用重建后的镜像。没有有效技能时不会注册 `skill_loader`，启动日志会说明原因。

`.env` 可配置 `STUDY_CODER_MAX_SKILL_CHARS=40000`（单个正文）和
`STUDY_CODER_MAX_ACTIVE_SKILL_CHARS=60000`（全部活动正文）。这两个预算独立于普通工具输出上限。
正文超限、累计超限，或系统提示、活动技能与工具 schema 的估算 token 达到窗口的 50% 时，
拒绝新增技能并返回明确工具错误，为任务和回复留出空间。上下文压缩阶段也会计入工具 schema。

成功加载后，工具历史只保存简短确认；完整正文按名称保存在 Agent 的 `active_skills`，每轮作为
系统前缀注入一次，不参与历史摘要。重复加载不重复注入，失败不会激活，`/clear` 会清空活动技能。
`--session <名称>` 保存消息和活动技能的名称、SHA-256 摘要，恢复时从当前镜像取回正文。
技能缺失、正文变化或预算不足会明确拒绝恢复，需使用原镜像或换新会话名称。

详细修复说明与阅读位置见 [技能加载修复说明](docs/skill-loader-fixes.md)。
