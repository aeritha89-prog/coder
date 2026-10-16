# study-coder

study-coder 是一个命令行编程助手，支持本地 Ollama 和远程 OpenAI 兼容模型。输入需求后，它可以阅读代码、修改文件、执行命令和运行测试。开发操作在 Docker 沙箱中完成，文件修改经过审阅后再写回工作目录。

## 项目设计

### 执行流程

```text
宿主机
  CLI → 加载配置与会话 → Agent → 上下文管理 → 模型请求
                         ↓ 工具调用
  DockerRuntime → 固定 Worker → Docker 内的 /workspace
                         ↓
                文件操作 / Shell / 任务管理 / 后台命令
                         ↓ 工具结果
                       Agent
                         ↓ 结束
  停止命令与服务 → 导出文件差异 → 审阅 → 应用到宿主工作区
```

模型请求、权限确认、会话保存和容器管理在宿主机运行。任务开始时，工作目录被复制到独立的 Docker volume，容器内路径为 `/workspace`。

工具请求以 JSON 传给容器中的 Worker，由它校验参数并执行。每个任务使用固定镜像 ID，保证 Worker 和技能来自同一版本。

Agent 根据工具结果继续读取、修改或验证代码，直到完成任务、达到轮次上限或遇到错误。如果计划还有未完成事项，或后台命令仍在运行、结果尚未读取，本轮会标记为未完成。

退出时先停止命令和服务，再导出文件差异。默认需要手动审阅和应用，也可以预先启用自动应用。应用前会检查宿主文件是否发生变化；中断时可通过事务日志回滚。

### 模块划分

| 模块 | 职责 |
| --- | --- |
| `cli.py`、`config.py` | 命令行入口、配置读取、交互和结果展示 |
| `agent.py`、`llm.py` | 模型调用、工具循环、完成条件、流式输出 |
| `context.py`、`session.py` | 请求上下文裁剪、增量摘要、会话存储与恢复 |
| `tools/` | 工具定义、参数校验、文件操作、任务管理和技能加载 |
| `sandbox/docker.py`、`sandbox/policy.py` | 容器生命周期与宿主控制的资源、网络、执行策略 |
| `sandbox/worker.py` | 容器内工具分发和执行 |
| `sandbox/background.py`、`sandbox/background_runner.py`、`sandbox/service_runner.py` | 后台命令、常驻服务及进程清理 |
| `sandbox/network_proxy.py`、`sandbox/transfer.py` | 域名代理、工作区传输与文件应用 |

## 功能

### 代码与命令工具

| 工具 | 功能 |
| --- | --- |
| `read_file` | 按行范围读取 UTF-8 文本，返回行号 |
| `glob` | 按模式查找文件 |
| `grep` | 使用正则表达式搜索文件内容 |
| `edit_file` | 替换唯一匹配的文本片段，返回修改差异 |
| `write_file` | 创建或覆盖文件，覆盖已有文件需显式指定 |
| `bash` | 在容器内执行 Shell 命令，支持管道、重定向和命令组合 |

文件工具使用相对于 `/workspace` 的路径，不允许访问工作区之外的文件。文本读写要求 UTF-8 编码，不接受包含 NUL 字节的文件。`bash` 的 `timeout_s` 可由模型指定，未指定时使用命令超时配置；同时受宿主的沙箱最大超时限制。

沙箱镜像内置 Python、uv、Git、curl、tar、编译工具、CMake、Node.js/npm、Java、Go、Rust/Cargo 和 ripgrep。项目所需的库需要在容器内安装或提前准备；宿主机安装的依赖不会自动带入沙箱。

### 计划任务管理

模型可以把工作拆成有依赖关系的任务，并通过工具维护进度：

- `create_task`、`task_list`、`get_task`：创建与查询任务。
- `update_task_dependencies`：更新前置依赖，检查不存在的任务、自依赖和循环依赖。
- `claim_task`、`complete_task`：按 `pending → in_progress → completed` 推进；前置任务完成后才能领取任务。
- `reset_task_progress`、`delete_task`：重置或删除任务，同时检查对依赖任务的影响。

计划保存在工作区的 `.tasks` 中。每次模型请求会带上简短的任务状态，详细信息按需查询。计划状态与命令执行状态分开管理：测试命令成功不会自动把对应计划任务标记为完成。

### 后台命令与常驻服务

后台命令默认开启，工具为 `start_background`、`get_background`、`wait_background` 和 `cancel_background`。模型可以启动耗时命令，在继续其他工作的同时查询、等待或取消它。

- 默认最多同时运行 4 个后台命令；它们共享同一容器、工作区和资源上限。
- 命令受超时限制，输出有容量限制，结束后记录成功、失败、超时或取消状态。
- 交互模式中，后台命令可以在两次输入之间继续运行；完成通知不会自动触发新的模型轮次。
- 退出时收集结果并停止未结束命令；恢复会话不会重启旧后台进程。

设置服务端口后，会注册 `start_service`、`stop_service`，用于管理长期运行的 Web 服务和检查端口就绪。服务进程在任务容器内运行，任务结束时停止。当前服务使用 isolated internal 网络；容器内就绪不等于宿主浏览器可访问，端口发布是否实际生效需要结合 Docker 网络状态确认。

### 三层记忆与会话

| 层级 | 内容 | 保存方式 |
| --- | --- | --- |
| 工作记忆 | 当轮目标、计划与后台状态、近期消息，以及本轮召回的资料 | 每次模型请求重新组装，受上下文预算限制 |
| 短期记忆 | 当前会话的增量摘要、已摘要消息组的哈希 | 随会话保存，恢复后复用 |
| 长期记忆 | 项目偏好、历史经验和可复用的操作方法 | 宿主 SQLite，跨会话保留，按项目隔离 |

长期记忆分为三类：

- **语义记忆 `semantic`**：偏好和稳定知识，例如回复语言、项目使用的框架。
- **情景记忆 `episodic`**：历史事实和经验，例如某次构建失败及其工具输出。每轮结束后默认自动归档最近的工具观察，不把模型自行声称的成功当成验证结果。
- **程序性记忆 `procedural`**：可复用的规则和操作方法，例如测试步骤、构建命令。

模型通过 `memory_remember` 保存或更新知识，使用相同的类别和键更新旧条目；通过 `memory_search` 查询，使用 `memory_forget` 删除。写入和删除经过工具权限判断。条目记录来源与更新时间，情景观察包含工具调用编号，可对应到任务对话。

长期条目通过 `load_policy` 指定加载方式，偏好主题、`key` 和正文均可自由表达：

- **`always`**：明确的长期偏好或工作规则，每次模型请求前独立读取，无需关键词命中。支持语义、程序性条目，单条正文最多 500 字符。先按 `priority`（0–100，默认 50）降序，再按更新时间降序选取。
- **`on_demand`**：默认策略，按当前需求检索项目事实、操作步骤和历史观察。英文按词、中文按双字匹配；有查询时仅保留关键词命中的条目。自动情景归档始终使用此策略。

常驻资料不占普通检索的 `top_k`，默认最多选取 8 条、使用 256 token；两路合计仍受长期记忆总预算约束，且不超过上下文窗口的 10%。常驻资料先装入预算，按需知识使用剩余额度；只加入完整条目。`always` 也受预算限制，超额时按优先级选取，并打印数量或预算跳过的统计。必要前缀拥挤时先去掉按需知识，保留已装入的常驻资料；必要输入仍超限时按上下文超限处理。

例如，用户明确表示“以后讲解函数时，先说明输入输出，再逐行讲解”后，模型可以调用：

```json
{
  "kind": "semantic",
  "key": "explanation.function_order",
  "content": "讲解函数时先说明作用、输入输出，再按执行顺序逐行解释。",
  "load_policy": "always",
  "priority": 80,
  "evidence": "以后讲解函数时，先说明输入输出，再逐行讲解"
}
```

宿主会核对 `evidence` 是当前用户输入中的非空原话。创建、修改或降级已有常驻条目都需要这项来源依据，省略 `load_policy` 更新时保留原策略，省略 `priority` 时保留原优先级。来源检查只核对引用确实来自用户，仍需模型正确判断长期意图；“这次简单说一下”等临时要求应留在会话中。CLI 由用户直接操作，可设置常驻条目而无需模型引用。每条记忆应只表达一项可独立更新的知识，避免将身份、偏好和项目事实混在一起。

旧数据库首次打开时在事务中增加加载策略、优先级和索引，原条目保持 `on_demand`，不根据旧正文自动提升为常驻。此版本的两类条目仍按工作区隔离，不跨项目共享。读取时不重复执行建表或写入元数据。更新、删除后，下次请求重新读取当前库，不复用旧的常驻内容。

所有召回内容以带边界提示的 `user` 消息进入模型输入，当前用户要求优先；文件内容、测试结果和服务状态仍需重新检查。一般模型笔记不视为自动验证过的事实，常驻资料也不授予工具权限。

完整对话保存在会话文件中。每次请求会根据上下文预算整理发送给模型的内容：

1. 加入基础提示、已激活技能、任务状态和后台命令状态。
2. 使用已有摘要替代已处理的历史前缀，再加入尚未摘要的消息和最近对话。
3. 达到预算阈值后先裁剪工具输出；仍过长则只对新产生、尚未处理的旧消息做增量摘要。
4. 始终把一次 assistant 工具调用与对应的全部工具结果作为完整消息组处理。
5. 如果仍然过长，继续压缩摘要并减少保留的近期消息；无法满足预算时返回错误。

预算包含提示、消息和工具定义。token 数为本地估算，`STUDY_CODER_CONTEXT_WINDOW_TOKENS` 控制请求预算，不会修改模型服务器的实际上下文窗口。

`--session` 可保存和恢复命名会话。默认状态目录为 `~/.study-coder/sandbox`：

```text
sandbox/
  memory/<工作区路径哈希>/memory.sqlite3  项目长期记忆
  history/sessions/<会话名>.json     命名会话
  <沙箱任务ID>/
    record.json                    生命周期与文件差异记录
    sessions/history.json          本次任务的对话检查点
    background/                    后台命令归档
```

恢复会话时会检查工作区路径、已激活技能，以及短期摘要对应的历史前缀。前缀匹配时直接复用摘要；不匹配时丢弃缓存，从完整历史重新建立。工作记忆中的任务和后台状态重新查询，不恢复旧进程。旧任务的文件仍需单独应用。

长期记忆默认最多 1000 条；达到容量时淘汰最旧的情景条目，语义和程序性条目需显式删除。显式写入会检查正文、键、标签和来源，拒绝可识别的凭据；自动情景归档先对完整内容脱敏，再截取预览。检查范围包括常见密钥字段、认证头、URL 用户名密码、AWS/GitHub 令牌、JWT 和私钥块。旧库条目在查询或召回时也会对这些格式脱敏，保留原 ID 供删除。这些规则无法识别所有秘密，保存知识时应写环境变量名，不应包含真实凭据。

自动召回的临时前缀不写入对话历史；显式记忆工具的调用参数和结果仍会进入历史，并可能参与短期摘要。`/forget`、`memory_forget` 和 `memory forget` 只删除长期数据库中的条目，不清除当前对话、已有会话文件、短期摘要或后台归档，也不保证从数据库磁盘页中安全擦除。完整原始工具输出仍以会话和后台归档为准。

### 技能加载

技能放在 `skills/<技能目录>/SKILL.md`，文件开头的 YAML frontmatter 定义 `name` 和 `description`，后面是技能正文。构建镜像时将技能复制为只读资产，任务启动时从该固定镜像读取目录。

模型通过技能名称和说明选择技能，再调用 `skill_loader` 加载正文。正文会加入后续请求的 system 提示，工具返回加载确认。同一个技能只加载一次；格式错误、内容过长或超出上下文预算时，返回错误原因。修改技能后需要重新构建镜像；恢复会话时会检查技能名称和内容哈希。

### 网络、权限与日志

- **网络默认关闭**：任务容器默认不能访问外网。`allowlist` 模式通过独立代理容器访问允许域名，限制目标端口、解析地址和总流量。
- **模型连接独立配置**：模型 API 请求从宿主发出，不受任务容器的网络禁用影响；宿主 `.env` 中的模型密钥不会自动传入工作区容器。
- **搜索按需开启**：`web_search` 需要单独启用和配置 `TAVILY_API_KEY`，在宿主执行。
- **资源受限**：容器使用非 root 用户、只读根文件系统，并限制 CPU、内存、进程数、命令输出和工作区逻辑容量。
- **敏感文件过滤**：导入与导出默认排除 `.env`、其他 `.env.*`、凭据和常见依赖缓存目录；`.env.example` 作为模板允许保留。
- **工具权限**：只读工具可以直接执行；修改文件、运行命令等操作需授权。交互模式支持单次允许、当前工具持续允许或拒绝；`--yes` 自动允许工具执行。
- **运行日志**：打印工具调用参数、返回结果、耗时等待、上下文压缩和后台状态；每轮任务结束时打印各工具的调用次数。

## Quick Start

以下命令使用 Windows PowerShell，在项目根目录执行。

### 1. 准备运行环境

需要 Python 3.11+、uv、正在运行且使用 Linux 容器的 Docker，以及支持工具调用的模型服务。使用域名代理或服务端口时，需要 Docker Engine 28+。

```powershell
uv --version
docker info
uv sync --extra dev
uv run study-coder --help
```

首次安装依赖和构建镜像需要网络。`uv sync --extra dev` 安装的是宿主开发环境，容器环境由镜像单独构建。

### 2. 配置模型

如果没有 `.env`，复制配置模板：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

本地 Ollama 配置：

```dotenv
STUDY_CODER_MODEL=qwen3.5:4b
STUDY_CODER_BASE_URL=http://127.0.0.1:11434/v1
STUDY_CODER_API_KEY=
STUDY_CODER_CONTEXT_WINDOW_TOKENS=32000
STUDY_CODER_MAX_ROUNDS=12
```

确认 Ollama 已运行并准备模型：

```powershell
ollama pull qwen3.5:4b
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

如果 Ollama 未启动，可打开应用，或在另一个终端运行 `ollama serve` 并保持运行。模型名称应与实际安装的名称一致；换模型时同步调整请求预算，并确认模型支持工具调用。

使用远程 OpenAI 兼容服务时，在 `.env` 中填写服务提供的模型名称、API 地址和密钥：

```dotenv
STUDY_CODER_MODEL=your-model-name
STUDY_CODER_BASE_URL=https://your-model-service.example/v1
STUDY_CODER_API_KEY=your-api-key
```

`.env` 从**启动目录**读取，`-C` 只指定待处理工作区，不改变配置文件读取位置。配置优先级为对应命令行参数、进程环境变量、`.env`、代码回退值。

### 3. 构建并检查沙箱

```powershell
uv run study-coder sandbox build
uv run study-coder sandbox doctor
```

构建会准备通用工具链、Worker 和技能，默认镜像名为 `study-coder-sandbox:general`。预检检查 Docker、Linux 容器环境、镜像和执行模块。更新镜像中使用的源码或技能后，应重新构建；宿主代码更新不会自动更新已有镜像。

### 4. 执行第一个任务

创建工作目录并启动对话：

```powershell
New-Item -ItemType Directory -Force -Path .\demo-workspace | Out-Null
uv run study-coder -C .\demo-workspace --stream --yes
```

- `-C`：指定已经存在的宿主工作区。
- `--stream`：边生成边显示模型文字。
- `--yes`：自动允许工具执行；未指定时按需询问权限。

进入 `study-coder>` 提示符后输入需求：

```text
创建 calculator.py，实现 divide(a, b)，b 为 0 时抛出 ZeroDivisionError；用标准库 unittest 编写 test_calculator.py，运行测试并报告结果。
```

一轮结束后可以继续提问、修改或等待后台任务。完成后输入 `exit` 或 `quit` 退出对话，程序会导出成果并打印沙箱任务 ID、文件变化数量和审阅、应用命令。**`--yes` 不会自动把成果写回宿主工作区。**

### 5. 审阅并应用成果

把下面的任务 ID 替换为实际输出值：

```powershell
$taskId = "替换为实际沙箱任务ID"
uv run study-coder sandbox review $taskId
uv run study-coder sandbox apply $taskId
uv run python -m unittest discover -s .\demo-workspace -p "test_*.py" -v
```

应用后，生成文件才会进入 `demo-workspace`。如果希望退出对话后直接应用，启动时添加 `--sandbox-apply auto`，或设置 `STUDY_CODER_SANDBOX_APPLY=auto`。

### 6. 交互与会话恢复

```powershell
uv run study-coder -C .\demo-workspace --session demo --stream
```

输入需求后，程序会按需询问工具权限。输入 `y` 允许本次调用，`a` 允许当前交互期间该工具的后续调用，`n` 拒绝。

| 输入 | 行为 |
| --- | --- |
| `/help` | 显示交互命令 |
| `/tokens` | 显示模型返回的累计输入、输出 token 用量；模型未提供时显示不可用 |
| `/clear` | 清除对话、工作记忆、短期记忆和已激活技能；保留长期记忆、工作区文件和后台命令 |
| `/memory [关键词]` | 查看当前项目的长期记忆；可带关键词查询 |
| `/forget 记忆ID` | 删除长期数据库中的一条记忆，保留会话及归档 |
| `exit`、`quit` | 退出并导出成果 |

再次用同一工作区和 `--session demo` 启动即可恢复对话。会话名称允许字母、数字、下划线和连字符，长度为 1–64 个字符。退出后仍需按输出提示应用成果。

也可以直接管理长期记忆，这些命令无需启动模型或 Docker：

```powershell
uv run study-coder memory remember -C .\demo-workspace --kind semantic --key reply_language --content "使用中文回复"
uv run study-coder memory remember -C .\demo-workspace --kind procedural --key test_command --content "在项目根目录运行 python -m unittest discover -v"
uv run study-coder memory remember -C .\demo-workspace --kind semantic --key explanation.function_order --content "先解释输入输出，再逐行讲解函数" --load-policy always --priority 80
uv run study-coder memory list -C .\demo-workspace --load-policy always
uv run study-coder memory list -C .\demo-workspace
uv run study-coder memory search "测试" -C .\demo-workspace
uv run study-coder memory forget mem_替换为实际编号 -C .\demo-workspace
```

长期条目在同一工作区的不同会话间共享，不会写入任务容器或随文件成果导出。`--session` 用于恢复对话和短期摘要；不指定会话名也能召回该项目的长期记忆。

### 7. 常用配置

模型相关配置列出 `.env.example` 中的值，沙箱配置列出默认值。未复制模板时，部分模型配置会使用不同的代码默认值。

| 环境变量 | 起始值 | 用途 |
| --- | --- | --- |
| `STUDY_CODER_MAX_ROUNDS` | `12` | 每次任务的模型循环上限 |
| `STUDY_CODER_CONTEXT_WINDOW_TOKENS` | `32000` | 本地估算的请求上下文预算 |
| `STUDY_CODER_REQUEST_TIMEOUT_S` | `60` | 单次模型请求超时 |
| `STUDY_CODER_COMMAND_TIMEOUT_S` | `60` | 前台命令默认超时，秒 |
| `STUDY_CODER_MAX_TOOL_OUTPUT_CHARS` | `12000` | 工具返回给模型的文本长度上限 |
| `STUDY_CODER_MEMORY_ENABLED` | `true` | 启用项目长期记忆及对应工具 |
| `STUDY_CODER_MEMORY_MAX_CONTEXT_TOKENS` | `1200` | 常驻与按需资料的合计 token 预算，同时不超过上下文窗口的 10% |
| `STUDY_CODER_MEMORY_TOP_K` | `6` | 每轮自动召回的候选条目数 |
| `STUDY_CODER_MEMORY_ALWAYS_MAX_CONTEXT_TOKENS` | `256` | 常驻资料预算上限，计入总预算，包含边界提示 |
| `STUDY_CODER_MEMORY_ALWAYS_MAX_ENTRIES` | `8` | 每轮独立读取的常驻候选条目上限 |
| `STUDY_CODER_MEMORY_MAX_ENTRIES` | `1000` | 每个项目的长期记忆容量 |
| `STUDY_CODER_MEMORY_AUTO_EPISODE` | `true` | 自动归档本轮工具观察为情景记忆 |
| `STUDY_CODER_SANDBOX_MAX_COMMAND_TIMEOUT_S` | `1800` | 前台与后台命令允许的最大超时 |
| `STUDY_CODER_SANDBOX_BACKGROUND_ENABLED` | `true` | 是否提供后台命令工具 |
| `STUDY_CODER_SANDBOX_MAX_BACKGROUND_JOBS` | `4` | 后台命令并发上限 |
| `STUDY_CODER_SANDBOX_CPUS` | `2` | 容器 CPU 配额 |
| `STUDY_CODER_SANDBOX_MEMORY_MB` | `4096` | 容器内存上限，MiB |
| `STUDY_CODER_SANDBOX_NETWORK` | `none` | `none` 禁用网络；`allowlist` 启用域名代理 |
| `STUDY_CODER_SANDBOX_SERVICE_PORTS` | 空 | 可管理的服务端口，逗号分隔 |
| `STUDY_CODER_SANDBOX_APPLY` | `review` | `review` 手动审阅应用；`auto` 自动应用 |

任务需要安装 Python 或 npm 依赖时，可在启动前配置域名代理：

```dotenv
STUDY_CODER_SANDBOX_NETWORK=allowlist
STUDY_CODER_SANDBOX_NETWORK_ALLOWLIST=pypi.org,files.pythonhosted.org,registry.npmjs.org,*.npmjs.org
```

白名单只允许列出的域名及匹配的子域名；镜像源、重定向、Git 仓库或额外下载地址需要分别加入。配置修改对下一次创建的任务生效。容器根文件系统只读，Python 项目通常需要在工作区创建虚拟环境后安装依赖。

如需搜索工具，另行配置：

```dotenv
STUDY_CODER_SANDBOX_ALLOW_WEB_SEARCH=true
TAVILY_API_KEY=your-search-api-key
```

### 8. 任务状态与恢复

```powershell
uv run study-coder sandbox list
uv run study-coder sandbox export $taskId
uv run study-coder sandbox recover $taskId
uv run study-coder sandbox cleanup $taskId
```

- `list`：查看已记录的沙箱任务。
- `export`：重试导出保留的工作卷，例如上次导出失败后。
- `recover`：恢复中断的文件应用事务，回滚后重新审阅、应用。
- `cleanup`：清理任务所属资源；未导出的成果会受到保护。显式添加 `--discard` 才允许丢弃未导出成果。

导出失败涉及下载的工具链或缓存时，可给已有任务追加排除规则：

```powershell
uv run study-coder sandbox export $taskId `
  --exclude "toolchain/jdk25" `
  --exclude "gradlehome/caches"
```

`--exclude` 可重复使用，支持目录名、相对路径和通配符（通配符请加引号）。
规则追加到任务原有排除规则，并保存在该任务中，后续重试无需重复传入。
规则用于容器 tar 预过滤和宿主解压过滤；被排除的宿主原文件不会被当作删除。
导出成功后使用 `sandbox review` 审阅，再用 `sandbox apply` 应用；已成功导出的任务不能新增排除规则。

以上命令按实际任务状态选用，同一任务的导出、应用、恢复和清理应串行执行。对话退出码为 `0` 表示最后一轮任务与退出收尾成功（未输入任务时正常退出也为 `0`）；`1` 表示最后一轮失败、未完成或退出收尾失败；`2` 表示配置或启动检查错误。
