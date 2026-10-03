# Sandbox 源码学习与阅读指南

这份指南对应当前 `src/study_coder/sandbox/` 实现。建议先用一个可丢弃的小工作区走通主线，再读服务和网络扩展。运行步骤见 [README 的 Docker Sandbox 章节](../README.md#docker-sandbox默认离线)，设计取舍和验收状态见 [sandbox-plan.md](sandbox-plan.md)。

## 1. 先建立整体模型

Sandbox 把“让模型修改项目”拆成五段：**导入快照 → 容器内执行 → 停止写入并导出 → 审阅差异 → 应用回宿主**。Agent 和模型接口始终在宿主进程；模型产生工具名和参数，宿主策略决定能否调用，Docker 后端负责把允许的工具请求送入容器。

```text
宿主：CLI / Agent / Policy / DockerRuntime / 任务记录
  原工作区 --create_import_archive--> import.tar --解包--> 独立 Docker volume
  Agent --JSON 请求--> docker exec --> Worker --> /workspace 中读写或运行 /bin/sh
  volume --只读 exporter + tar--> extract_export --> 宿主 staging 目录
  导入快照 + 导出快照 --> changes --> review --> apply_changes --> 原工作区
                                   失败或中断 --> recover_apply

可选：任务容器 --内部网络--> 独立网络代理 --白名单域名--> 外网
可选：Worker --start_service--> service_runner --前台服务--> 宿主回环端口
```

这里有三个不同的文件位置：原工作区是用户的项目；容器 `/workspace` 是独立 volume；宿主任务目录（默认 `~/.study-coder/sandbox/<任务ID>/`）保存记录、导出文件和应用日志。`--yes` 授权 Agent 调用有副作用的工具；默认 `review` 模式仍需单独 `sandbox apply` 才改原工作区。

## 2. 阅读前只需理解的 Docker 概念

| 概念 | 在本项目里的具体角色 | 读源码时看什么 |
| --- | --- | --- |
| 镜像 | `general.Dockerfile` 构建的固定 Linux 工具和 Python Worker | 为什么任务固定使用预检得到的 image ID |
| 容器 | 每项任务一个长期存活的 task container；每次工具调用再 `docker exec` 一个短命 Worker | 容器停止是否意味着所有写入者停止 |
| volume | `/workspace` 的可写副本；不把宿主项目以可写挂载交给 Agent | 导入、导出和清理的时机 |
| 只读根文件系统与 tmpfs | 根文件系统只读，`/tmp` 可暂存进程状态 | 命令真正可写的位置在哪里 |
| 网络 | 默认 `none`；预览或白名单场景使用 internal 网络 | 代理与任务容器谁有外网出口 |
| `docker exec` | 宿主启动容器内固定 Worker，stdin/stdout 传一次 JSON | 模型命令如何与宿主 Docker 管理命令隔开 |

不用先学完 Docker CLI。能解释“镜像、容器、volume、挂载、exec”五者的关系，就可以开始读代码。

## 3. 推荐阅读顺序

下表的“完成标志”可当作每一轮学习后的自测。路径均相对项目根目录。

| 顺序 | 文件与入口 | 阅读重点 | 完成标志 |
| --- | --- | --- | --- |
| 0 | `README.md` 的 Docker Sandbox 章节；`sandbox/images/general.Dockerfile` | 默认离线、构建与预检、镜像里的 Worker 与非 root 用户 | 能说出为何先 `build`、再 `doctor` |
| 1 | `src/study_coder/sandbox/base.py`；`tools/__init__.py`；`agent.py::_execute_call`；`cli.py::main` | 工具名单、Agent 授权、哪些调用走 `runtime.execute_tool`，哪些留在宿主 | 能从 Agent 工具请求追到 DockerRuntime |
| 2 | `sandbox/policy.py`、`sandbox/manifest.py` | `.env` 与环境变量优先级、资源上限、任务状态持久化、资源归属 | 能指出模型的 `timeout_s` 为什么不能扩大宿主策略上限 |
| 3 | `sandbox/transfer.py::_excluded → _safe_name → create_import_archive` | 导入过滤、链接/路径检查、文件与总量上限、SHA-256 快照 | 能解释 `.env` 与 `.env.example` 各自会怎样处理 |
| 4 | `sandbox/docker.py::doctor → build_image → DockerRuntime.start` | 镜像预检、创建任务目录/volume/容器、网络选项、启动失败清理 | 能画出原工作区到 `/workspace` 的单向路径 |
| 5 | `sandbox/docker.py::execute_tool → _bounded_exec`；`sandbox/worker.py::main → execute` | JSON 协议、工具路由、输出限制、宿主 watchdog、容器内参数验证 | 能定位一次 `bash` 调用的请求与返回 |
| 6 | `sandbox/worker.py::_shell` 与文件工具分支 | `/bin/sh -c`、进程组、超时、输出收集、工作区逻辑配额 | 能说明命令超时后为什么要处理子进程 |
| 7 | `sandbox/docker.py::export_changes → review_task → apply_task`；`transfer.py::extract_export → changes → apply_changes → recover_apply` | 停止写入者、流式校验、暂存、差异、备份/日志/回滚 | 能解释原工作区在何时才改变，以及冲突时发生什么 |
| 8 | `sandbox/docker.py::cleanup_task/recover_task`；`service_runner.py`、`network_proxy.py`；`tools/sandbox_service.py` | 生命周期恢复、预览服务、代理认证/域名/SNI/流量限制 | 能区分普通任务、常驻服务和可选联网任务 |

建议每步都按 **输入 → 输出 → 状态变化 → 失败路径** 记录笔记。例如读 `create_import_archive` 时，输入是宿主工作区路径、tar 路径和排除规则；输出是 `{相对路径: SHA-256}`；副作用是生成 tar；发现链接、文件变化或超限则抛 `SandboxError`。不要把“返回值”和“写入文件/创建 Docker 资源”混在一起。

## 4. 按调用链走一遍普通任务

假设可丢弃工作区最初只有 `hello.txt`，内容为 `old`，Agent 把它改为 `new` 并新增 `notes.txt`。

1. `cli.main` 载入配置与 `SandboxPolicy`，调用 `DockerRuntime.start`。`doctor` 验证 daemon、Linux 模式、镜像 ID 和 Worker；任务记录先写为 `importing`。
2. `create_import_archive` 遍历宿主工作区，过滤默认敏感文件，读取稳定的普通文件，把 `hello.txt` 写进 `import.tar`，同时记录旧哈希 `H(old)`。Docker 把 tar 解入新 volume，然后启动挂载该 volume 的任务容器，记录变为 `running`。
3. Agent 先经过工具授权。`DockerRuntime.execute_tool` 将 `write_file` 及其参数序列化为 JSON；`_bounded_exec` 启动 `docker exec -i ... python -m study_coder.sandbox.worker`。Worker 在 `/workspace` 写入；宿主的 `hello.txt` 此时仍是 `old`。
4. CLI 结束任务时调用 `export_changes`，先停止任务容器并确认停止，再用只读挂载 volume 的临时容器输出 tar。`extract_export` 逐个校验成员路径、类型、大小和内容，将允许的文件写进宿主 staging 目录并得到新哈希。`changes` 比较旧、新哈希，得到 `modified: hello.txt`、`added: notes.txt`；记录成为 `awaiting_apply`。
5. `review_task` 读取 staging 与原工作区，生成文本 diff。用户执行 `sandbox apply` 后，`apply_changes` 再次核对原文件仍是 `H(old)`，备份旧文件、保存日志，然后按记录写入新文件，最终记录成为 `applied`。若原文件已被外部改动，校验失败；如应用中途失败，则尝试 `recover_apply` 回滚。

状态主线：`importing → running → exporting → awaiting_apply → applying → applied`。失败和中断还可能出现 `failed`、`cleanup_failed`、`export_failed`、`stopped`、`apply_recovery_required`、`discarded`、`cleaned`。读 `manifest.py` 时，注意状态写进 `record.json`；它不是只存在于 Python 对象里的变量。

## 5. 再走一遍 `bash` 与超时

模型传 `{"command": "python -m pytest", "timeout_s": 120}` 时：

1. Agent 将请求交给 `DockerRuntime.execute_tool`。`timeout_s` 必须是 1～86400 的整数，且不能超过 `SandboxPolicy.max_command_timeout_s`。不传时使用 `Settings.command_timeout_s` 默认值。
2. `_bounded_exec` 把 JSON 送到 Worker，并独立收集 Docker 命令的 stdout/stderr；宿主 watchdog 在 `timeout_s + 5` 秒后取消任务容器，处理 Worker 卡住或失联的情况。
3. `worker.execute` 再检查请求范围。`_shell` 使用 `subprocess.Popen(["/bin/sh", "-c", command], cwd=/workspace, start_new_session=True)`；因此管道、重定向等由**容器内**的 shell 解释，宿主管理 Docker 的 `_control` 不使用 shell。
4. `_shell` 同时读取两路输出并控制总字节数；周期扫描工作区大小/文件数；到达 `timeout_s` 时终止命令进程组并清理后代。正常退出也清理后代，以免后台命令继续改 volume。

要区分两层超时：Worker 的命令超时负责普通命令生命周期；宿主 `_bounded_exec` 的额外 watchdog 负责 Worker 或 Docker 协议失联。工作区容量是周期扫描的逻辑限制，不是 volume 的硬配额。

## 6. 导入、导出与应用分别防什么

| 位置 | 主要检查 | 失败后的处理 |
| --- | --- | --- |
| `create_import_archive` | 默认排除、普通文件、非链接、单文件/总量/数量限制、读前后文件身份 | 不启动正常任务；清理已创建的本任务资源 |
| `extract_export` | tar 路径安全、大小写冲突、拒绝链接和设备、文件与总量限制、流式读取完整性 | `export_failed`；volume 留待重试或人工处理 |
| `changes` | 导入快照与导出快照的哈希差异 | 只形成清单，不写宿主项目 |
| `apply_changes` | 宿主原文件仍匹配导入哈希、staging 仍匹配导出哈希、目标未逃出工作区 | 尝试按日志回滚；遇第三方改动保留冲突现场 |

`.env` 及常见凭据在导入、导出都默认排除；`.env.example` 是例外，可以作为模板保留。导出时仍先把 tar 成员计入数量和大小上限，再决定是否跳过排除项。单文件上限是 16 MiB；这是导入/导出规则，与策略里的工作区总量上限分开。

## 7. 最后读可选能力

**常驻预览服务：** `tools/sandbox_service.py` 给模型提供工具 schema；真正执行仍由 Worker 的 `_start_service`、`_stop_service` 负责。它只接受策略允许的端口，启动 `service_runner.py`，等端口监听后返回 `service_id`。`service_runner` 维持前台命令并监控工作区；停止时 Worker 核对 PID 与启动时间，避免 PID 复用误伤进程。此能力需预先配置 `STUDY_CODER_SANDBOX_SERVICE_PORTS`。

**受控网络：** `DockerRuntime.start` 在白名单模式创建内部任务网络与独立代理容器，给任务容器设置代理变量。`network_proxy.py` 的学习顺序是 `Budget → ProxyServer.allowed → ProxyHandler.handle/_authorize → _connect → _read_client_hello → _relay`。依次理解认证令牌、域名匹配、地址检查、HTTPS CONNECT 的 SNI 匹配与总流量/时间限制。默认只接受公网地址；`trust_fake_ip_dns` 是宿主明确启用时对本机透明代理 `198.18.0.0/15` 虚拟地址的限定例外。代理不解密 HTTPS。默认 `network=none` 时不用先读这个文件；宿主 `web_search` 开关与容器联网是两回事。

## 8. 可复现的小练习

只在临时的可丢弃目录练习，并保持默认 `review` 模式。下列命令在项目根目录的 PowerShell 执行；根据本机实际路径修改练习目录。

```powershell
New-Item -ItemType Directory -Force D:\study\sandbox-learning-demo
Set-Content -Path D:\study\sandbox-learning-demo\hello.txt -Value old
uv run study-coder sandbox build --profile general
uv run study-coder sandbox doctor --profile general
uv run study-coder -C D:\study\sandbox-learning-demo --sandbox docker --sandbox-profile general --yes -p '把 hello.txt 的内容改为 new，并新增 notes.txt 写一句说明'
uv run study-coder sandbox list
$taskId = '把上一条命令输出的 32 位任务 ID 填在这里'
uv run study-coder sandbox review $taskId
```

先检查原目录的 `hello.txt` 仍为 `old`，再从输出的任务 ID 找到宿主任务目录和 `record.json`，对照 `snapshot`、`exported`、`diff` 与 `status`。确认差异正确后执行 `uv run study-coder sandbox apply $taskId`，再检查原目录变成 `new`。如果模型或 Docker 当时不可用，只读源码追踪同一条调用链，勿把静态理解当作实际集成验收。

下一轮可以分别练：让 `bash` 执行 `sleep 3` 并设置 `timeout_s=1`；在任务运行期间从宿主修改 `hello.txt`，观察应用冲突；按 README 配置一个预览端口后观察 `start_service` 到 `service_runner` 的路径。不要在真实项目上故意触发冲突练习。

## 9. 完成学习时应能回答

1. 为什么任务容器拿到的是 volume，而不是宿主工作区的可写挂载？
2. 哪些校验发生在导入前、导出时和应用前？三个时刻分别信任什么？
3. `bash` 使用了 shell，为什么宿主 Docker 管理命令仍不会解释模型输入？
4. 一次命令超时、Worker 失联、导出失败和应用冲突，各自留下什么状态与可恢复资料？
5. 哪些资源按 `study-coder.task` 标签回收？为什么不能只凭资源名称删除？
6. `.env`、`.env.example`、宿主模型 key 与容器代理 token 各处于什么边界？

源码注释已按这些问题补在对应函数附近。建议第二遍阅读时先遮住注释，尝试自己回答输入、输出和失败路径，再核对注释与代码实际条件。
