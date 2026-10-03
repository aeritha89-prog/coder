# 沙箱导出失败排查报告：硬链接成员被拒 + 排除模式失效 + 错误信息被掩盖

> 面向修改代码的 agent。本文件包含现象、根因、可复现步骤、修复建议与验证方式。
> 涉及文件：`src/study_coder/sandbox/transfer.py`、`src/study_coder/sandbox/docker.py`

---

## 1. 结论速览

用户在 Docker 沙箱模式下跑完一个真实任务（Vue + FastAPI 项目），任务正常完成，但收尾导出阶段报错，重试同样失败：

```
Sandbox export failed: Docker export failed: 1
```

该错误信息**不含任何有效信息**。经复现取证，真实原因是被掩盖的：

```
SandboxError: unsupported export entry: frontend/node_modules/@esbuild/linux-x64/bin/esbuild
```

| # | 缺陷 | 定位 | 性质 | 严重度 |
|---|---|---|---|---|
| A | `extract_export` 的**类型检查早于排除检查**，导致被排除目录里的硬链接/符号链接让整个导出失败 | `transfer.py:219-230` | 逻辑顺序错误 | **高**（必现、阻断交付） |
| B | `_EXPORT_EXCLUDE` 模式**顶层锚定**，匹配不到嵌套的 `frontend/node_modules`，使大量本应排除的成员进入导出流 | `docker.py:32-33`、`docker.py:517-518` | 规则失效 / 清单漂移 | **高**（触发器） |
| C | 失败路径**先看 `returncode` 再看异常**，Windows 上 `Popen.kill()` 会把 returncode 置为 `1` 且 stderr 为空，真实异常被丢弃 | `docker.py:555-557`、`docker.py:558-575` | 可观测性缺陷 | **高**（让 A/B 无法诊断） |

三者叠加：B 让不该进流的内容进了流 → A 在其中遇到硬链接直接判死 → C 把 A 的报错替换成一个毫无信息量的 `1`。

---

## 2. 可复现现象

### 2.1 触发方式

```powershell
uv run study-coder -C D:\study\test -p "<任意会在工作区产生 npm install / pip install 的任务>" --sandbox docker --stream --yes
```

任务执行阶段全部正常（agent 完成、`run()` 返回 0），失败发生在任务结束后的 `finish()` → `runtime.export_changes()` 阶段。因此表现为"明明正常结束了，最后还报错"。

### 2.2 重试仍失败

```
PS> uv run study-coder sandbox export 50ff31e8b1984eb591821fde6ced062a
Sandbox error: Docker export failed: 1
```

两次失败**截断点完全一致**（见 2.4），说明是内容决定的确定性故障，不是 Docker/磁盘抖动。

### 2.3 取证：用项目自己的 `extract_export` 消费真实 tar 流

把 `docker run ... tar -cf -` 的 stdout 直接喂给 `study_coder.sandbox.transfer.extract_export`，即可绕过 `docker.py` 的错误掩盖，拿到真实异常：

```python
import subprocess, tempfile
from pathlib import Path
from study_coder.sandbox.transfer import extract_export

image = "sha256:<task 记录中的 image_id>"
volume = "study-coder-<task_id>"
excludes = ["./.venv", "./node_modules", "./.cache", "./.npm", "./__pycache__", "./.pytest_cache", "./.uv-cache"]
cmd = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
       "--security-opt", "no-new-privileges", "--user", "10001:10001",
       "--mount", f"type=volume,src={volume},dst=/workspace,readonly", image,
       "tar", "-C", "/workspace", "-cf", "-", *[f"--exclude={e}" for e in excludes], "."]
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
staging = Path(tempfile.mkdtemp()) / "staging"
try:
    print("EXTRACT_OK", len(extract_export(p.stdout, staging, ())))
except Exception as exc:
    print("EXTRACT_FAILED:", type(exc).__name__, "->", exc)
    print("extracted_so_far=", sum(1 for _ in staging.rglob("*")))
finally:
    p.kill(); p.wait()
    print("tar_returncode=", p.returncode)
```

实际输出：

```
EXTRACT_FAILED: SandboxError -> unsupported export entry: frontend/node_modules/@esbuild/linux-x64/bin/esbuild
extracted_so_far= 2989
tar_returncode= 1
```

### 2.4 现场量化数据

| 指标 | 实测值 |
|---|---|
| tar 归档成员总数（含目录） | 6495 |
| 实际落地 staging 的条目数 | 2989（2751 文件 + 238 目录） |
| `./frontend/node_modules` 起始成员序号 | 5604 |
| 归档中符号链接成员 | 6 个，位于 `frontend/node_modules/.bin/` |
| 失败成员 | `frontend/node_modules/@esbuild/linux-x64/bin/esbuild` |

---

## 3. 根因分析

### 3.1 缺陷 A：类型检查早于排除检查（致命）

`transfer.py` 的 `extract_export` 流式循环：

```python
count += 1
if count > max_files * 2:
    raise SandboxError("export file count limit exceeded")
if not member.isdir() and not member.isfile():          # ← L219-220：先判类型
    raise SandboxError(f"unsupported export entry: {relative}")
if member.isfile():
    # 即便成员随后被排除，也先计入归档预算，防止大量无用数据耗尽资源。
    file_count += 1
    ...
if _excluded(relative, extra_exclude):                   # ← L229-230：后判排除
    continue
```

`node_modules/@esbuild/linux-x64/bin/esbuild` 与 `node_modules/esbuild/bin/esbuild` 是**同一个文件的硬链接**（`ls -l` 显示链接数为 2，均为 9707520 字节；esbuild 安装脚本用硬链接把平台二进制挂到主包下）。tar 对这种成员记为 `LNKTYPE`，而 `TarInfo.isfile()` 只对 `REGTYPE` 为真、`islnk()`/`issym()` 均为假，于是被 L219 直接判定为"不支持的条目"并抛出致命错误。

**关键点**：`node_modules` 本来就在 [_EXCLUDED_PARTS](file:///d:/study/Project/coder/src/study_coder/sandbox/transfer.py#L26-L28) 里，[_excluded](file:///d:/study/Project/coder/src/study_coder/sandbox/transfer.py#L38-L50) 也是按路径组件匹配的——也就是说，只要 L229 的排除判断排在 L219 之前，这条成员会被正常跳过，导出根本不会失败。**排除判定必须优先于类型判定**：被排除的文件不会进入交付物，它们是否可移植与该次导出无关。

注意 L222 的注释说明"排除了也要计入预算"是**有意设计**，修改顺序时不要把它一起改掉（预算统计应保持在排除判断之前）。

### 3.2 缺陷 B：导出排除模式顶层锚定，漏掉嵌套依赖目录（触发器）

```python
_EXPORT_EXCLUDE = ("./.venv", "./node_modules", "./.cache", "./.npm", "./__pycache__",
                   "./.pytest_cache", "./.uv-cache")
```

这些模式被拼成 `--exclude=<name>` 传给 tar（[docker.py:517-518](file:///d:/study/Project/coder/src/study_coder/sandbox/docker.py#L517-L518)）。tar 的 `--exclude` 是把模式与**成员全名**做 glob 匹配，而 `docker run ... tar -C /workspace ... .` 产生的成员名形如 `./frontend/node_modules/...`——模式 `./node_modules` 是**锚定在顶层**的，匹配不到嵌套层级（实测 `./frontend/node_modules` 从第 5604 个成员起完整进入归档，共约 890 个成员）。

这里暴露了一个更深的问题：**导出排除规则存在两份互相独立、语义不同的声明**：

| 位置 | 声明 | 匹配语义 |
|---|---|---|
| `transfer.py:26-28` `_EXCLUDED_PARTS` | `node_modules` 等 14 项 | 按路径组件匹配，**能**命中嵌套 |
| `docker.py:32-33` `_EXPORT_EXCLUDE` | `./node_modules` 等 7 项 | 与成员全名 glob 匹配，**不能**命中嵌套 |

两处不一致（正是评审中已提示过的"清单漂移"）。本次事故里 tar 侧漏排除是触发器，而 transfer 侧本可兜底却被 A 的顺序问题挡住了。

补充：`create_import_archive` 走的是 Python 侧 `_excluded`，不使用 tar 的 `--exclude`，因此该问题**只影响导出方向**（但方向性覆盖差异本身也值得统一）。

### 3.3 缺陷 C：错误上报被 `returncode` 覆盖（不可诊断）

```python
        except Exception as exc:
            if process.poll() is None:
                process.kill()                    # ← Windows 上把 returncode 置为 1
            process.wait()
            ...
            stderr_reader.join(timeout=2)
            if process.returncode:                # ← L570：优先看 returncode
                detail = stderr_bytes.decode("utf-8", errors="replace")[:500].strip()
                raise SandboxError(f"Docker export failed: {detail or process.returncode}") from exc
            if isinstance(exc, SandboxError):     # ← L573：真正的原因到这里已被跳过
                raise
```

机制：

1. `extract_export` 抛出 `SandboxError("unsupported export entry: ...")`，此时 tar 仍在向管道写数据，`process.poll() is None`。
2. 于是执行 `process.kill()`。**Windows 没有信号机制，CPython 用 `TerminateProcess(handle, 1)` 实现 `kill()`，导致 `returncode == 1`**（POSIX 上是 `-9`）。
3. stderr 为空（真正的原因在 Python 层，不在 docker CLI），`detail or process.returncode` 落到 `1`。
4. L570 分支抢先命中，抛出的消息是 `Docker export failed: 1`，L573 的 `isinstance(exc, SandboxError): raise` 永远执行不到。原始异常仅作为 `__cause__` 保留，CLI 只打印 `str(exc)`，所以用户看不到。

同样的问题也存在于成功路径的 [L555-557](file:///d:/study/Project/coder/src/study_coder/sandbox/docker.py#L555-L557)（该处 stderr 可能为空，消息同样无信息量）。

**同类模式提示**：`docker.py` 中 `_bounded_exec` 附近（约 L395-415）也存在 `kill()` + `if process.returncode:` 的上报组合，建议一并复核是否会把工具执行的真实失败掩盖成 `1`。

---

## 4. 修复建议

### 4.1 必做 1：调整 `extract_export` 的判定顺序（`transfer.py`）

目标：排除判定排在类型判定之前；预算统计保持在排除判定之前（保留既有设计意图）。

```python
        for member in archive:
            relative = _safe_name(member.name)
            if not relative:
                continue
            folded = relative.casefold()
            if folded in seen:
                raise SandboxError(f"duplicate or case-colliding archive path: {relative}")
            seen.add(folded)
            count += 1
            if count > max_files * 2:
                raise SandboxError("export file count limit exceeded")
            if member.isfile():
                # 即便成员随后被排除，也先计入归档预算，防止大量无用数据耗尽资源。
                file_count += 1
                if member.size > MAX_FILE_BYTES or file_count > max_files:
                    raise SandboxError(f"export file or count limit exceeded: {relative}")
                total += member.size
                if total > max_total_bytes:
                    raise SandboxError("workspace export exceeds configured workspace limit")
            # 排除判定必须先于类型判定：被排除的成员不会进入交付物，
            # 其中的符号链接/硬链接不应让整个导出失败。
            if _excluded(relative, extra_exclude):
                continue
            if not member.isdir() and not member.isfile():
                raise SandboxError(f"unsupported export entry: {relative}")
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            ...
```

即：把原 L229-230 的两行整体上移到原 L219-220 之前。**不要**顺带放宽类型检查（见 4.3）。

### 4.2 必做 2：修正导出排除模式并消除清单漂移（`docker.py` / `transfer.py`）

短期（最小改动）：把 `_EXPORT_EXCLUDE` 改为按**组件名**书写，使嵌套层级也能命中：

```python
_EXPORT_EXCLUDE = (".venv", "node_modules", ".cache", ".npm", "__pycache__",
                   ".pytest_cache", ".uv-cache")
```

注意：tar 的排除匹配语义（是否锚定、是否匹配 basename）在不同实现下有差异，**修改后必须用集成测试实证**嵌套目录确实被排除（见第 5 节），不要只靠推理。

建议（更彻底）：把导出排除规则**收敛为单一来源**，由 `transfer.py` 的 `_EXCLUDED_PARTS` 派生出传给 tar 的 `--exclude` 参数，避免两份清单再次漂移。tar 侧排除只作为"省流量的优化"，正确性必须由 `extract_export` 的 `_excluded` 兜底——两条路径都要成立。

### 4.3 必做 3：错误上报优先保留原始异常（`docker.py`）

```python
        except Exception as exc:
            if process.poll() is None:
                process.kill()
            process.wait()
            try:
                _remove_owned_resource("container", exporter, self.record["task_id"])
            except SandboxError:
                pass
            self.record["status"] = "export_failed"
            save_record(self.directory, self.record)
            stderr_reader.join(timeout=2)
            detail = stderr_bytes.decode("utf-8", errors="replace")[:500].strip()
            if isinstance(exc, SandboxError):
                # 导出失败的真实原因在 Python 侧。process.kill() 在 Windows 上会把
                # returncode 置为 1，不能用它覆盖真正的错误。
                raise SandboxError(f"{exc} (exporter stderr: {detail})" if detail else str(exc)) from exc
            if process.returncode:
                raise SandboxError(f"Docker export failed: {detail or process.returncode}") from exc
            raise SandboxError(f"Docker export could not be verified: {exc}") from exc
```

同时建议把成功路径 L555-557 的消息改为在 stderr 为空时给出更明确的提示，例如：

```python
            if process.returncode:
                raise SandboxError(
                    f"Docker export failed (exporter exit {process.returncode}): "
                    f"{stderr_bytes.decode('utf-8', errors='replace')[:500].strip() or 'no stderr'}")
```

补充（可观测性）：失败时把 `detail`、`process.returncode`、以及首个失败成员路径写入 `record`（例如 `record["export_error"]`），并清理半成品 `staging` 目录——目前失败路径不删除 `export-<hex>`，每次重试都会残留一个（本次事故残留了两个，共 45MB×2）。若选择清理，需保留"可用于人工取证"的选项或至少记录日志。

### 4.4 建议 4：补充单元测试（`tests/`）

当前缺少对 `extract_export` 成员类型处理的单测。建议用手工构造的 tar 流覆盖：

1. 普通文件 + 目录 → 正常导出；
2. **硬链接成员（`LNKTYPE`）且路径命中 `_EXCLUDED_PARTS`（如 `frontend/node_modules/...`） → 应被跳过，不抛错**（本次事故的回归用例）；
3. 硬链接/符号链接成员且路径**未被排除** → 应抛 `SandboxError("unsupported export entry: ...")`（保持既有安全立场）；
4. 符号链接成员位于被排除目录内 → 应被跳过。

### 4.5 可选 5：是否支持工作区内链接

若要支持沙箱内的符号链接/硬链接（很多构建工具会生成），安全前提是：解析后目标必须仍位于工作区内且为普通文件，并且不得绕过排除规则（例如链接指向被排除目录）。**默认建议维持"拒绝"的现状**，仅作为独立需求评估；不要为了修这个 bug 而放宽类型检查。

---

## 5. 验证方式

### 5.1 单元测试（首选，快且确定）

见 4.4。用 `tarfile.open(fileobj=io.BytesIO(...), mode="w")` 构造含 `LNKTYPE`/`SYMTYPE` 成员的 tar 字节流，喂给 `extract_export`，断言行为。

### 5.2 集成测试

在 `tests/integration/test_sandbox_runtime.py` 现有真实容器用例基础上扩展：在工作区里制造一个嵌套依赖目录（如 `frontend/node_modules/`），其中包含硬链接与符号链接（例如 `ln` 两个文件），然后断言：

- `export_changes()` 成功；
- staging 中不出现 `frontend/node_modules`；
- `record["diff"]` 不含被排除路径。

### 5.3 手工端到端复现（本次使用过，可直接复用）

```powershell
# 1. 确认卷内存在硬链接（本次为 2 个，均在 node_modules 内）
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges `
  --user 10001:10001 --mount type=volume,src=study-coder-<task_id>,dst=/workspace,readonly `
  <image_id> sh -c "find /workspace -type f -links +1"

# 2. 跑一次真实沙箱任务，确认导出不再报 Docker export failed: 1
uv run study-coder -C D:\study\test -p "..." --sandbox docker --stream --yes
```

修复后预期：即使 `frontend/node_modules` 内含硬链接，`export_changes()` 也能成功，且交付物中不含 `node_modules`。

---

## 6. 回归风险

| 改动 | 风险 | 处理 |
|---|---|---|
| 调整判定顺序 | 若把预算统计一并移到排除判断之后，会削弱"防止无用数据耗尽预算"的保护 | 严格按 4.1 的顺序：**预算 → 排除 → 类型** |
| 修改排除模式 | tar 排除匹配语义可能不如预期，导致仍然漏排除 | 必须用集成测试实证嵌套目录被排除；正确性由 transfer 侧 `_excluded` 兜底 |
| 放宽类型检查 | 若改为"解引用链接"，可能把链接目标（甚至工作区外文件）内容导出，存在信息泄漏 | 本次修复**不放宽**类型检查 |
| 错误上报改动 | 调用方/测试可能断言消息前缀 `Docker export failed:` | 检查 `tests/` 中对导出失败消息的断言并同步更新 |

---

## 7. 现场数据与已有产物的抢救

本次事故的现场（供人工复现用）：

| 项 | 值 |
|---|---|
| task_id | `50ff31e8b1984eb591821fde6ced062a` |
| volume | `study-coder-50ff31e8b1984eb591821fde6ced062a` |
| image_id | `sha256:fc607fbdddad6066e2de97b6668246973de4f38d3bd552b7e85414a96616d767` |
| 状态 | `export_failed`（volume、任务容器、会话历史均保留） |

导出失败时**故意不删除 volume**，因此 agent 的工作成果是安全的，可在修复前用以下方式抢救（删除本是构建产物的 `node_modules`，它本来就会被导出排除，不影响交付内容）：

```powershell
docker run --rm --user 10001:10001 `
  --mount type=volume,src=study-coder-50ff31e8b1984eb591821fde6ced062a,dst=/workspace `
  sha256:fc607fbdddad6066e2de97b6668246973de4f38d3bd552b7e85414a96616d767 `
  rm -rf /workspace/frontend/node_modules
uv run study-coder sandbox export 50ff31e8b1984eb591821fde6ced062a
```

---

## 8. 附带发现（不是本次 bug，需要产品决策）

agent 在工作区里执行 `pip install` 时，容器环境 `HOME=/workspace`，依赖会装进 **`/workspace/.local`**（本次约 45MB、2700 个文件）。`.local` **不在**任何排除名单中，因此：

1. 它会被完整导出并 apply 到目标工作区，污染交付目录；
2. 它会占用导出预算（本次 2989 条落地条目里绝大多数来自 `.local`）；
3. 大量工具产生的其余家目录产物（`.cache`、`.config`、`.npm` 等）存在同类问题。

建议评估是否将 `HOME` 指向容器内的 tmpfs（如 `/tmp/home`，注意其 64MiB sizelimit 可能不够）或把 `.local`/`.config` 等纳入默认排除清单。这是独立于本 bug 的设计决策，不要与本修复混在一个改动里。
