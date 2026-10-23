# Terminal-Bench 2.1 测试结果

coder 在 Terminal-Bench 2.1 的 89 道任务中通过 75 道，**通过率为 84.27%（75/89）**。

## 测试配置

| 项目 | 记录 |
| --- | --- |
| 数据集 | `terminal-bench/terminal-bench-2-1`，89 道不同任务 |
| 模型 | `deepseek-flash`，74 份已保存轨迹 |
| Agent | `study-coder` 0.1.0，入口为 `evals.tb.agent:StudyCoderAgent` |
| 评测副本基线 | `c2c009c8591baba8489b64af26359d711fb39a14`，含本地适配改动 |
| Harbor / Docker | 0.24.0 / 29.8.0（原报告记录） |
| 并发 | 3 |
| 容器与 verifier | 89 份 lock 均为 Docker、`timeout_multiplier=1.0`、verifier `shared` 模式 |
| 评测记录的时间范围 | 2026-10-21 19:10:49 至 2026-10-23 14:57:48，北京时间 |
| 长期记忆 / 技能 / 后台工具 | 关闭 |

## 整体结果

| 指标 | 数值 | 统计范围 |
| --- | ---: | --- |
| 通过 | 75 / 89 | verifier `reward == 1.0` |
| 未通过 | 14 / 89 | 包含 1 个未产出 reward 的任务 |
| 主通过率 | **84.27%** | 全部 89 道题 |
| AgentTimeoutError | 15 个 trial | 其中 5 道通过，10 道未通过 |
| 正常结束但未通过验收 | 4 道 | 原指标分类为 `model` |
| Adapter 异常 / 服务限制标记 | 0 / 0 | 原汇总器分类 |
| 已记录输入 token | 181,512,412 | 74 个有 token 记录的 trial |
| 已记录输出 token | 3,073,808 | 74 个有 token 记录的 trial |
| 已记录工具调用 | 3,481 次 | 74 份已保存轨迹 |
| 工具调用成功率 | 89.89%（3,129/3,481） | 以 `is_error` 判定 |
| Agent 执行时长 | 平均 740.5 秒，中位 463.1 秒 | 89 个 trial 的 Agent 阶段 |
| Agent 执行时长合计 | 65,908.6 秒，约 18.31 小时 | 89 个 trial 的 Agent 阶段累计时长 |
| 轨迹覆盖 | 74 / 89 | 15 个超时 trial 无已保存轨迹 |

### Agent 结束状态

| Agent 结束状态 | 通过 | 未通过 | 合计 |
| --- | ---: | ---: | ---: |
| 正常返回，`stop_reason=completed` | 70 | 4 | 74 |
| `AgentTimeoutError` | 5 | 10 | 15 |
| 合计 | 75 | 14 | 89 |

## 未通过任务

### 超时分类：10 道

| 任务 | Agent 用时（秒） | Reward | 原始记录中的补充情况 |
| --- | ---: | ---: | --- |
| `code-from-image` | 1,200.0 | 0.0 | 无已保存轨迹 |
| `extract-moves-from-video` | 1,800.0 | 0.0 | 无已保存轨迹 |
| `gcode-to-text` | 900.0 | 0.0 | 无已保存轨迹 |
| `make-doom-for-mips` | 900.0 | 0.0 | 无已保存轨迹 |
| `model-extraction-relu-logits` | 900.0 | 0.0 | 无已保存轨迹 |
| `qemu-alpine-ssh` | 900.0 | 0.0 | 无已保存轨迹 |
| `raman-fitting` | 900.0 | 0.0 | 无已保存轨迹 |
| `torch-pipeline-parallelism` | 900.0 | 未产出 | `verifier_result` 为 null |
| `train-fasttext` | 3,600.0 | 0.0 | 无已保存轨迹 |
| `tune-mjcf` | 900.0 | 0.0 | 无已保存轨迹 |

另有 5 个超时但通过的任务：`adaptive-rejection-sampler`、`compile-compcert`、`regex-chess`、`rstan-to-pystan`、`sqlite-with-gcov`。

### 正常结束但验收失败：4 道

| 任务 | Agent 用时（秒） | Verifier 中可确认的失败 |
| --- | ---: | --- |
| `filter-js-from-html` | 1,312.2 | 修改了应保持原样的干净 HTML，`test_clean_html_unchanged` 失败 |
| `git-multibranch` | 221.9 | HTTPS 多分支部署测试失败 |
| `gpt2-codegolf` | 186.3 | 预期文件 `/app/gpt2.c` 不存在 |
| `pytorch-model-cli` | 192.9 | 预测结果与预期值不一致 |

## 工具调用

| 工具 | 调用次数 | 失败次数 | 成功率 |
| --- | ---: | ---: | ---: |
| `bash` | 3,123 | 237 | 92.4% |
| `write_file` | 125 | 63 | 49.6% |
| `edit_file` | 94 | 11 | 88.3% |
| `read_file` | 66 | 33 | 50.0% |
| `create_task` | 21 | 4 | 81.0% |
| `complete_task` | 18 | 1 | 94.4% |
| `claim_task` | 17 | 0 | 100.0% |
| `task_list` | 8 | 2 | 75.0% |
| `update_task_dependencies` | 5 | 0 | 100.0% |
| `glob` | 3 | 0 | 100.0% |
| `write`，无效工具名 | 1 | 1 | 0.0% |

### 文件工具失败统计

| 指标 | 数值 |
| --- | ---: |
| `bash` 调用占比 | 89.72% |
| 文件工具调用 | 285 次 |
| 文件工具失败 | 107 次 |

| 文件工具失败类型 | 次数 |
| --- | ---: |
| 拒绝绝对路径 | 73 |
| 容器无可用 Python，文件工具不可用 | 20 |
| 文件已存在，覆盖条件不满足 | 7 |
| 文件不存在 | 1 |
| 其他 | 6 |

## 数据文件

| 文件 | 内容 |
| --- | --- |
| [methodology.md](methodology.md) | 评测接入与统计口径 |
| [data/results.json](data/results.json) | 汇总、覆盖情况及 89 条评测记录 |
| [data/tasks.csv](data/tasks.csv) | 逐题结果、异常标记、已知用量、时间戳和任务版本引用 |
| [data/tools.csv](data/tools.csv) | 各工具的调用、失败和成功率 |
| [data/provenance.json](data/provenance.json) | 404 份已读取源文件的相对路径、大小和 SHA-256 |
| [verify_results.py](verify_results.py) | 统计与源文件核验脚本 |
| [adapter/evals/tb/](adapter/evals/tb/) | Harbor 适配器、Worker 打包和指标汇总 |
| [adapter/tests/](adapter/tests/) | 工具协议与 Worker 归档测试 |
| [adapter/.env.example](adapter/.env.example) | 评测配置模板 |

## 复现方法

### 1. 准备环境

宿主机需要 Python 3.12 或更高版本、uv，以及运行 Linux 容器的 Docker。下面的命令使用 PowerShell，均在项目根目录执行；容器平台为 Linux x86_64。

```powershell
uv sync --python 3.12 --extra dev
uv tool install --python 3.12 harbor==0.24.0 --with-editable .
harbor --version
docker info
docker compose version
```

Harbor 运行环境通过 `--with-editable .` 加载当前项目源码。目录中的适配器使用 `coder` 包，入口为 `evals.tb.agent:CoderAgent`。

### 2. 配置模型

项目根目录没有 `.env` 时，复制评测模板：

```powershell
if (-not (Test-Path .env)) {
    Copy-Item benchmarks/terminal-bench-2.1/adapter/.env.example .env
}
```

在 `.env` 中填写模型名称、OpenAI 兼容接口地址和 API key，并设置评测参数：

```dotenv
CODER_MODEL=
CODER_BASE_URL=
CODER_API_KEY=
CODER_WORKSPACE=.
CODER_MAX_ROUNDS=2000
CODER_COMMAND_TIMEOUT_S=1800
CODER_MAX_TOOL_OUTPUT_CHARS=12800
CODER_CONTEXT_WINDOW_TOKENS=200000
CODER_REQUEST_TIMEOUT_S=60
CODER_MEMORY_ENABLED=false
```

本次测试记录的模型名称为 `deepseek-flash`。模型服务需支持工具调用；请求从宿主机发出。

### 3. 注册适配器并准备 Worker

```powershell
$tbAdapter = (Resolve-Path benchmarks/terminal-bench-2.1/adapter).Path
$env:PYTHONPATH = $tbAdapter + [IO.Path]::PathSeparator + $env:PYTHONPATH
uv run python -c "from evals.tb.build_worker import build_worker_zip; print(build_worker_zip())"
uv run python -m pytest benchmarks/terminal-bench-2.1/adapter/tests -q
```

Worker 归档生成在 `.cache/evals/worker.pyz`。Harbor 在每个任务的 setup 阶段将归档上传到容器，并探测 Python 和工作目录；缺少可用 Python 的任务使用 bash。

部分任务的 setup 会上传 Linux 版 uv。下载 [uv 0.9.5 的 Linux x86_64 发行包](https://github.com/astral-sh/uv/releases/tag/0.9.5)：

```powershell
New-Item -ItemType Directory -Force .cache/evals/uv-linux | Out-Null
Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz" -OutFile .cache/evals/uv-linux.tar.gz
tar -xzf .cache/evals/uv-linux.tar.gz -C .cache/evals/uv-linux
```

解压后的可执行文件位置为 `.cache/evals/uv-linux/uv-x86_64-unknown-linux-gnu/uv`，由适配器按任务自动上传。

### 4. 单题检查

```powershell
$tbModel = (uv run python -c "from coder.config import load_settings; print(load_settings().model)").Trim()
harbor run `
    -d terminal-bench/terminal-bench-2-1 `
    --agent evals.tb.agent:CoderAgent `
    -m $tbModel `
    --env docker `
    --include-task-name "*fix-git" `
    --n-concurrent 1 `
    --n-attempts 1 `
    --timeout-multiplier 1.0 `
    --job-name tb-smoke `
    --jobs-dir .cache/harbor-jobs
```

### 5. 全量测试

```powershell
harbor run `
    -d terminal-bench/terminal-bench-2-1 `
    --agent evals.tb.agent:CoderAgent `
    -m $tbModel `
    --env docker `
    --n-concurrent 3 `
    --n-attempts 1 `
    --timeout-multiplier 1.0 `
    --job-name tb-full `
    --jobs-dir .cache/harbor-jobs
```

Harbor 下载数据集、准备任务容器、执行 Agent 和 verifier，结果保存在 `.cache/harbor-jobs/tb-full/`。长期记忆、技能、后台工具和原生沙箱导出在评测适配器中关闭。

### 6. 汇总结果

```powershell
uv run python -m evals.tb.metrics .cache/harbor-jobs/tb-full --out .cache/harbor-jobs/tb-full-report
```

| 输出 | 内容 |
| --- | --- |
| `tb-full-report/metrics.json` | 逐题结果和汇总指标 |
| `tb-full-report/metrics.md` | 通过率、工具调用、执行时长和逐题结果 |
| `tb-full/<trial>/result.json` | Harbor 验收结果与执行信息 |
| `tb-full/<trial>/agent/coder-session.json` | 已正常返回的 Agent 对话和工具轨迹 |
| `tb-full/<trial>/verifier/test-stdout.txt` | Verifier 测试输出 |

通过率按全部任务计算：`reward == 1.0` 为通过，其余计入未通过。模型响应、依赖下载及容器环境会影响实际结果。
