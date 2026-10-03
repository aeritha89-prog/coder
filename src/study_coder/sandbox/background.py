"""宿主后台管理器：唯一轮询者、有限结果归档和逐项交付。

模型命令只通过注入的容器控制 RPC 执行；本模块没有本地 shell fallback。
状态锁保护内存记录，RPC 锁串行化控制请求及关联操作；命令运行期间不持有它们。
wait 使用条件变量等待，Condition.wait 会暂时释放状态锁，让监测线程继续更新。

jobs 的结构是 job_id -> entry；entry 与容器中的 record 含义不同：
    record：最近确认的容器状态，字段释义见 background_runner 模块开头。
    archived：终态和保留日志已经写入宿主结果文件及索引。
    observed：Agent 读取终态后，完整工具调用组已保存，再确认已交付。
    pruned：容器日志已回收或不再有可读来源；宿主归档仍可继续查询。
    archive_bytes：该任务在宿主保留的日志字节数；尚未归档时按 output_limit 预留。
    output_incomplete：容器提前停止等原因导致日志无法完整收集，区别于容量截断。
任务结束、归档完成、Agent 已读取是三个边界，不能用同一个布尔值代替。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from study_coder.sandbox.background_runner import (
    ARCHIVE_BUDGET,
    LOG_BUDGET,
    MAX_RECORDS,
    TERMINAL,
    atomic_write,
    job_id,
    regular_bytes,
    validate_status,
)
from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.policy import SandboxPolicy
from study_coder.types import ToolResult


class ControlRejected(SandboxError):
    """控制端明确拒绝了请求，区别于响应丢失后启动结果未知。"""


def safe_display(text: str) -> str:
    """工具数据不能向终端写入 ANSI/OSC 控制序列。"""
    # OSC 可改变终端标题／链接，CSI 可移动光标／清屏；显示前去除这些序列。
    # 这只处理终端显示，原始结果文件和工具数据不经过此函数改写。
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))


def result_json(value: dict[str, Any], maximum: int) -> str:
    """返回不超过 maximum 字符的 JSON；预算过小则拒绝，避免截成非法 JSON。

    maximum 是模型工具结果的字符预算，与容器日志字节预算是两个限制。
    单任务保留核心状态，优先剪短输出；任务列表必要时删尾项并报告 omitted。
    """
    # 复制顶层，替换输出／列表时不直接修改调用方字典。
    value = dict(value)
    def encode() -> str:
        return json.dumps(value, ensure_ascii=False)
    while len(encode()) > maximum:
        tail = value.get("output_tail")
        if tail:
            # 留最近的诊断；response_truncated 表示本次工具响应还做了一次裁剪。
            value["output_tail"] = tail[len(tail) // 2 + 1:]
            value["response_truncated"] = True
        elif value.get("jobs"):
            value["jobs"] = value["jobs"][:-1]
            value["omitted"] = value.get("omitted", 0) + 1
        else:
            raise SandboxError("max_tool_output_chars is too small for background metadata")
    return encode()


class BackgroundManager:
    """一个沙箱生命周期的后台命令；归档与模型读取状态相互独立。"""

    def __init__(self, directory: Path, policy: SandboxPolicy,
                 rpc: Callable[[dict[str, Any]], dict[str, Any]],
                 abort: Callable[[], None], *, interval: float = 2.0) -> None:
        """创建一个沙箱的管理器，恢复宿主索引；此时不主动启动监测线程。

        directory：当前沙箱的宿主任务目录，不是模型可写的 /workspace。
        policy：用户给出的可信资源上限；rpc：注入的容器短控制调用。
        abort：控制／清理持续失效时停止整个容器的兜底回调。
        interval：监测轮询间隔，单位秒；不是命令超时或 RPC 超时。
        """
        self.directory = directory / "background"
        self.policy = policy
        self.rpc = rpc
        self.abort = abort
        self.interval = interval
        self.jobs: dict[str, dict[str, Any]] = {}  # 本沙箱的全部已登记任务，含已结束项。
        self._lock = threading.RLock()  # 状态锁：保护 jobs、计数和通知队列；允许嵌套调用。
        self._io = threading.RLock()  # 控制锁：避免监测、查询、启动和取消交错操作同一来源。
        self._condition = threading.Condition(self._lock)  # 状态变化后唤醒 wait 的等待者。
        self._stop = threading.Event()  # 请求停止宿主监测线程；设置它本身不会停止命令。
        self._thread: threading.Thread | None = None  # 懒创建的唯一周期监测线程。
        self._closing = False  # 收尾已开始，拒绝新的后台启动。
        self._closed = False  # 收尾已完成，使 shutdown 可以重复调用。
        self._fault = ""  # 最近控制／归档异常；非空时不能把运行状态视为完全已知。
        self._failures = 0  # 连续失败的监测次数；一次成功 refresh 会清零。
        self._busy = False  # CLI 当前正在流式输出或询问权限，应暂存终端通知。
        self._notified: set[str] = set()  # 本管理器已显示完成通知的编号，用于通知去重。
        self._notifications: list[str] = []  # busy 期间排队的显示文本，不是模型消息。
        self.notifier: Callable[[str], None] = lambda text: print(text, file=sys.stderr, flush=True)
        index = self.directory / "index.json"
        if index.exists():
            # index 记录交付状态；读取旧结果不等于重启旧后台命令。
            saved = json.loads(regular_bytes(index, 2 * 1024**2))
            if not isinstance(saved, dict) or len(saved) > MAX_RECORDS:
                raise SandboxError("invalid background archive index")
            for identifier, entry in saved.items():
                job_id(identifier)
                if (not isinstance(entry, dict) or not isinstance(entry.get("record"), dict)
                        or entry["record"].get("job_id") != identifier):
                    raise SandboxError("invalid background archive entry")
                validate_status(entry["record"])
                if any(type(entry.get(k)) is not bool for k in ("observed", "archived", "pruned")):
                    raise SandboxError("invalid background delivery state")
                # result.json 是该归档的提交标记。如果它已落盘但索引未更新，校验
                # 编号、执行意图和日志长度后向前恢复；不把旧 running 当作最终结果。
                path = self.directory / identifier
                result_path = path / "result.json"
                if not entry["archived"] and result_path.exists():
                    if path.is_symlink():
                        raise SandboxError("invalid background archive path")
                    result = validate_status(json.loads(regular_bytes(result_path, 65536)))
                    if (result["job_id"] != identifier or result["command"] != entry["record"]["command"]
                            or result["timeout_s"] != entry["record"]["timeout_s"]
                            or result["status"] not in TERMINAL):
                        raise SandboxError("background archive differs from registered command")
                    size = sum(len(regular_bytes(path / name, result["output_limit"]))
                               for name in ("output.head", "output.tail"))
                    if size != result["output_retained_bytes"]:
                        raise SandboxError("background archive is incomplete")
                    entry.update(record=result, archived=True, archive_bytes=size,
                                 output_incomplete=result.get("output_incomplete", False))
                self.jobs[identifier] = entry

    def _persist(self) -> None:
        """保存全部 entry 的宿主索引；调用方应持有 _lock。"""
        # 调用方持有状态锁；不会与轮询线程并发写半份索引。
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(self.directory / "index.json",
                     json.dumps(self.jobs, ensure_ascii=False).encode("utf-8"))

    def _query(self, request: dict[str, Any]) -> dict[str, Any]:
        """串行发送一次内部控制请求，返回字典或抛出分类后的 SandboxError。"""
        with self._io:
            response = self.rpc(request)
        if not isinstance(response, dict):
            raise SandboxError("invalid background control response")
        if "error" in response:
            message = "background control: " + str(response["error"])[:500]
            if response.get("rejected") is True:
                # 只有控制端明确保证未创建任务时，才能安全认定没有启动副作用。
                raise ControlRejected(message)
            raise SandboxError(message)
        return response

    def _ensure_monitor(self) -> None:
        """首次需要时创建监测器；每个管理器最多一条，wait 不另开轮询线程。"""
        with self._lock:
            if self._thread is None and not self._closing:
                self._thread = threading.Thread(target=self._monitor, name="background-monitor",
                                                daemon=True)
                self._thread.start()

    def _monitor(self) -> None:
        """周期刷新所有未归档项；一次查询失败允许恢复，不立即取消整个容器。"""
        # Event.wait 让关闭请求可以提前打断间隔，无需等 time.sleep 自然结束。
        while not self._stop.wait(self.interval):
            with self._lock:
                needed = any(not entry["archived"] for entry in self.jobs.values())  # 是否仍有结果待收集。
            if not needed:
                continue
            try:
                self.refresh()
            except (SandboxError, OSError, ValueError) as exc:
                with self._condition:
                    self._fault = str(exc)
                    self._failures += 1
                    failures = self._failures
                    self._condition.notify_all()
                if failures == 1:
                    self._notify("[后台任务] 状态查询或归档异常：" + safe_display(str(exc))[:200])
                # 文件归档失败保留原容器供重试；控制通道持续失效则确保停止写入。
                if failures >= 3 and isinstance(exc, SandboxError):
                    # 磁盘 OSError 保留原日志来源供重试；协议／清理持续失败则兜底停容器。
                    try:
                        self.abort()
                    except (SandboxError, OSError) as failure:
                        self._notify("[后台任务] 容器停止失败：" + safe_display(str(failure))[:200])
                    self._stop.set()

    def _notify(self, text: str) -> None:
        """发送或暂存终端通知；不修改 Agent.history，不发起模型请求。"""
        with self._lock:
            if self._busy:
                if len(self._notifications) < MAX_RECORDS + 4:
                    self._notifications.append(text)
                return
            self.notifier(text)

    def pause_notifications(self, busy: bool) -> None:
        """CLI 进入／离开流式输出或授权询问；恢复显示时按入队顺序排空。"""
        with self._lock:
            self._busy = busy
            if not busy:
                queued, self._notifications = self._notifications, []
                for text in queued:
                    self.notifier(text)

    def _accept(self, raw: Any) -> str:
        """验证一项容器状态并更新宿主缓存，返回其编号；不标记结果已读取。"""
        try:
            record = dict(validate_status(raw))
        except ValueError as exc:
            raise SandboxError("invalid background status: " + str(exc)) from exc
        identifier = record["job_id"]
        with self._condition:
            entry = self.jobs.get(identifier)  # 只能接受宿主此前登记过的任务。
            if entry is None:
                raise SandboxError("background response contains an unregistered job")
            if record["command"] != entry["record"]["command"] or record["timeout_s"] != entry["record"]["timeout_s"]:
                raise SandboxError("background response differs from registered command")
            # 已归档终态是宿主稳定结果，不能被陈旧 running 或取消响应覆盖。
            if not entry["archived"]:
                entry["record"] = record
            self._condition.notify_all()
        return identifier

    def _read(self, identifier: str) -> tuple[dict[str, Any], bytes, bytes]:
        """返回 (状态字典, 保留头部, 保留尾部)，解码有界 base64 日志。"""
        response = self._query({"op": "read", "job_id": identifier})
        self._accept(response.get("record"))
        record = dict(validate_status(response["record"]))
        try:
            head = base64.b64decode(response["head"], validate=True)
            tail = base64.b64decode(response["tail"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxError("invalid background log encoding") from exc
        if len(head) + len(tail) > record["output_limit"]:
            raise SandboxError("background log exceeds reserved budget")
        return record, head, tail

    def _archive(self, identifier: str) -> None:
        """把新终态和日志归档到宿主，索引提交成功后显示一次完成通知。"""
        with self._lock:
            if self.jobs[identifier]["archived"]:
                return
        record, head, tail = self._read(identifier)
        if record["status"] not in TERMINAL:
            return
        path = self.directory / job_id(identifier)
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve()):
            raise SandboxError("invalid background archive path")
        # 日志先写、result.json 后写：存在完整结果标记才可在构造时向前恢复。
        atomic_write(path / "output.head", head)
        atomic_write(path / "output.tail", tail)
        atomic_write(path / "result.json", json.dumps(record, ensure_ascii=False).encode("utf-8"))
        with self._lock:
            entry = self.jobs[identifier]
            previous = dict(entry)  # 索引失败时恢复内存状态，继续保留容器来源以便重试。
            entry.update(record=record, archived=True, archive_bytes=len(head) + len(tail))
            try:
                self._persist()
            except OSError:
                self.jobs[identifier] = previous
                raise
            if identifier not in self._notified:
                self._notified.add(identifier)
                preview = safe_display(record["command"])[:80].replace("\n", " ")
                self._notify(f"[后台任务] 已结束，命令={preview!r}，开始={record['created_at']:.3f}，"
                             f"状态={record['status']}，退出码={record['exit_code']}，"
                             f"耗时={record['elapsed_s']}s\n"
                             f"{safe_display((head + tail).decode('utf-8', errors='replace')[-800:])}\n"
                             f"[后台任务] 结果归档：{path}")

    def refresh(self) -> None:
        """一次批量状态查询；各个新终态分别归档，不一次弹出后丢弃。"""
        with self._io:
            with self._lock:
                # expected 是宿主尚需向容器确认／归档的编号，不含稳定宿主结果。
                expected = {identifier for identifier, e in self.jobs.items() if not e["archived"]}
            if not expected:
                with self._lock:
                    if self.jobs:
                        self._persist()
                        self._fault = ""
                        self._failures = 0
                return
            records = self._query({"op": "poll"}).get("records")
            if not isinstance(records, list) or len(records) > MAX_RECORDS:
                raise SandboxError("invalid background status list")
            found: set[str] = set()  # 本次 poll 实际返回的编号，用于检测重复和记录丢失。
            for raw in records:
                identifier = self._accept(raw)
                if identifier in found:
                    raise SandboxError("duplicate background status")
                found.add(identifier)
            for identifier in expected - found:
                with self._lock:
                    entry = self.jobs[identifier]
                    if time.time() - entry["record"]["created_at"] < 10:
                        continue  # 宿主意图已登记，容器握手仍在进行。
                raise SandboxError("registered background job disappeared")
            for identifier in found:
                with self._lock:
                    record = dict(self.jobs[identifier]["record"])
                if record["status"] in TERMINAL:
                    self._archive(identifier)
                    if not record["cleanup_confirmed"]:
                        self.abort()
                        raise SandboxError("background cleanup could not be confirmed")
            with self._condition:
                self._fault = ""
                self._failures = 0
                self._persist()
                self._condition.notify_all()

    def _prune_observed(self) -> None:
        """回收已归档、已交付且已清理的容器日志；宿主归档和编号仍保留。"""
        with self._lock:
            candidates = [key for key, e in self.jobs.items()
                          if e["archived"] and e["observed"] and not e["pruned"]
                          and e["record"]["cleanup_confirmed"]]
        for identifier in candidates:
            self._query({"op": "prune", "job_id": identifier})
            with self._lock:
                self.jobs[identifier]["pruned"] = True
                self._persist()

    def start(self, command: str, timeout_s: int) -> dict[str, Any]:
        """校验和预留预算，登记意图后发启动 RPC，返回该任务的有限元数据。

        command 在容器执行；timeout_s 由模型选择，但不能超过可信 policy 上限。
        启动回应丢失时保留同一编号供追查，避免重跑一个可能已有副作用的命令。
        """
        if not isinstance(command, str) or not command.strip() or len(command) > 16384 or "\0" in command:
            raise ValueError("command must be nonempty text of at most 16384 characters")
        if type(timeout_s) is not int or not 1 <= timeout_s <= self.policy.max_command_timeout_s:
            raise ValueError("timeout_s exceeds sandbox limit or is not a positive integer")
        identifier = "job_" + uuid.uuid4().hex  # 宿主生成执行编号，模型不能指定 PID 或路径。
        request = {"op": "start", "job_id": identifier, "command": command,
                   "timeout_s": timeout_s, "max_jobs": self.policy.max_background_jobs,
                   "output_limit": self.policy.command_output_bytes,
                   "workspace_max_bytes": self.policy.workspace_max_bytes,
                   "workspace_max_files": self.policy.workspace_max_files}
        if len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > 65536 - 1024:
            # command 的字符数与 UTF-8 字节数不同；给后续状态字段留 1 KiB 空间。
            raise ValueError("background command exceeds serialized status budget; shorten command")
        with self._io:
            self._prune_observed()
            with self._lock:
                if self._closing:
                    raise SandboxError("background manager is closing")
                if len(self.jobs) >= MAX_RECORDS:
                    raise SandboxError("background lifetime record limit reached")
                # active 是当前占用运行名额的条数；未交付但已清理的终态不占名额。
                active = sum(e["record"]["status"] not in TERMINAL or not e["record"]["cleanup_confirmed"]
                             for e in self.jobs.values())
                if active >= self.policy.max_background_jobs:
                    raise SandboxError("background concurrency limit reached")
                # reserved：容器尚未回收的容量预留；archived：宿主已保留字节加未收集
                # 项的最大预留。这里 archived 是预算计数，区别于 entry['archived'] 标志。
                reserved = sum(e["record"]["output_limit"] for e in self.jobs.values() if not e["pruned"])
                archived = sum(e.get("archive_bytes", e["record"]["output_limit"]) for e in self.jobs.values())
                if reserved + self.policy.command_output_bytes > LOG_BUDGET or archived + self.policy.command_output_bytes > ARCHIVE_BUDGET:
                    raise SandboxError("background log budget is full")
                record = {"version": 1, "job_id": identifier, "command": command,
                          "timeout_s": timeout_s, "output_limit": self.policy.command_output_bytes,
                          "status": "starting", "created_at": time.time(), "elapsed_s": 0.0,
                          "reason": "", "runner_pid": None, "runner_start": None, "exit_code": None,
                          "cleanup_confirmed": False, "logs_pruned": False, "cancel_requested": False,
                          "output_truncated": False, "output_total_bytes": 0, "output_retained_bytes": 0}
                self.jobs[identifier] = {"record": record, "observed": False,
                                         "archived": False, "pruned": False}
                self._persist()  # 在任何可能有副作用的 RPC 前保存意图。
            try:
                response = self._query(request)
                self._accept(response.get("record"))
            except ControlRejected as exc:
                # 明确拒绝发生在控制端登记/启动之前；不泄漏名额，也不重试命令。
                with self._lock:
                    record.update(status="failed", reason="start_rejected: " + str(exc)[:500],
                                  cleanup_confirmed=True)
                    self._empty_archive(identifier, record, incomplete=False)
                self._notify("[后台任务] 启动被拒绝：" + safe_display(str(exc))[:200])
            except SandboxError as exc:
                # 保留同一编号；后续 poll 查清事实，不自动再执行一次命令。
                with self._lock:
                    self._fault = f"start result unknown for {identifier}: {exc}"
                raise SandboxError(self._fault) from exc
            finally:
                self._ensure_monitor()
        self._notify(f"[后台任务] 已启动，命令={safe_display(command)[:80]!r}，超时={timeout_s}s")
        with self._lock:
            return self._metadata(self.jobs[identifier]["record"])

    @staticmethod
    def _metadata(record: dict[str, Any]) -> dict[str, Any]:
        """挑选工具结果字段；内部 PID 和路径不作为模型的控制参数。"""
        value = {k: record[k] for k in ("job_id", "status", "exit_code", "reason", "elapsed_s",
                                        "output_truncated", "output_total_bytes",
                                        "output_retained_bytes", "cleanup_confirmed")}
        # 命令摘要只返回在 tool 数据中；系统状态前缀使用 state() 的独立白名单。
        value.update(command_preview=record["command"][:160], timeout_s=record["timeout_s"])
        return value

    def get(self, identifier: str, tail_chars: int) -> dict[str, Any]:
        """返回状态和末尾最多 tail_chars 字符；查询不删除结果，也不自动确认交付。"""
        job_id(identifier)
        with self._lock:
            if identifier not in self.jobs:
                raise ValueError("background job does not belong to this runtime")
            archived = self.jobs[identifier]["archived"]
        if not archived:
            self.refresh()
        with self._lock:
            entry = dict(self.jobs[identifier])
            record = dict(entry["record"])
        if entry["archived"]:
            # 稳定归档优先从宿主读取，容器已回收日志或停止后仍可查看。
            path = self.directory / identifier
            if path.is_symlink():
                raise SandboxError("invalid background archive path")
            raw = b"".join(regular_bytes(path / name, record["output_limit"])
                           for name in ("output.head", "output.tail"))
        else:
            record, head, tail = self._read(identifier)
            raw = head + tail
        value = self._metadata(record)
        # 原始输出可能不是 UTF-8，替换坏字节保证工具有文本；tail_chars=0 返回空串。
        value["output_tail"] = raw.decode("utf-8", errors="replace")[-tail_chars:] if tail_chars else ""
        value["output_incomplete"] = entry.get("output_incomplete", False)
        return value

    def wait(self, identifier: str, tail_chars: int) -> dict[str, Any]:
        """等待一项结果归档后调用 get；阻塞宿主工具调用，不重复调用 LLM。

        状态仍由唯一监测器刷新，等待 A 时也能监测 B，且不持有 RPC 锁。
        """
        job_id(identifier)
        self._ensure_monitor()
        with self._lock:
            if identifier not in self.jobs:
                raise ValueError("background job does not belong to this runtime")
            record = self.jobs[identifier]["record"]
            remaining = max(0, record["created_at"] + record["timeout_s"] - time.time())  # 估算剩余秒数。
        # created_at 是跨进程 Unix 时间；转换成单调 deadline，后续等待不依赖时钟调整。
        deadline = time.monotonic() + remaining + 15  # 执行结束后另给查询、清理和归档宽限。
        heartbeat = time.monotonic() + 30  # 下次“等待中”通知时间；不触发模型请求。
        while True:
            with self._condition:
                entry = self.jobs[identifier]
                if entry["archived"]:
                    break
                if self._failures >= 3:
                    raise SandboxError(self._fault or "background control unavailable")
                if time.monotonic() >= deadline:
                    self.abort()
                    raise SandboxError("background runner exceeded its cleanup deadline")
                # wait 暂时释放 _lock，被通知或短超时后重新取得锁检查状态。
                self._condition.wait(timeout=min(self.interval, 1.0))
            if time.monotonic() >= heartbeat:
                self._notify("[后台任务] 等待中，命令=" + safe_display(record["command"])[:80])
                heartbeat = time.monotonic() + 30
        return self.get(identifier, tail_chars)

    def cancel(self, identifier: str) -> dict[str, Any]:
        """请求停止指定 runner，再确认终态和归档；重复取消已归档项直接返回结果。"""
        job_id(identifier)
        with self._lock:
            if identifier not in self.jobs:
                raise ValueError("background job does not belong to this runtime")
            if self.jobs[identifier]["archived"]:
                return self.get(identifier, 4000)
        self._query({"op": "cancel", "job_id": identifier})
        deadline = time.monotonic() + 8  # 等待该任务清理确认的上限，不是新的执行超时。
        while time.monotonic() < deadline:
            self.refresh()
            with self._lock:
                if self.jobs[identifier]["archived"]:
                    return self.get(identifier, 4000)
            time.sleep(0.1)
        self.abort()
        raise SandboxError("background cancellation could not confirm cleanup")

    def acknowledge(self, identifiers: list[str]) -> None:
        """Agent 保存完整工具调用组后确认终态已交付；终端通知不会调用此方法。"""
        with self._lock:
            for identifier in identifiers:
                if identifier in self.jobs and self.jobs[identifier]["archived"]:
                    self.jobs[identifier]["observed"] = True
            if identifiers:
                self._persist()

    def pending(self) -> tuple[str, ...]:
        """返回不能宣布完成的原因：仍在运行、结果未交付或控制状态未知。"""
        with self._lock:
            reasons = []
            if any(e["record"]["status"] not in TERMINAL for e in self.jobs.values()):
                reasons.append("background_running")
            if any(e["record"]["status"] in TERMINAL and not e["observed"] for e in self.jobs.values()):
                reasons.append("background_unobserved")
            if self._fault:
                reasons.append("background_unknown")
            return tuple(reasons)

    def state(self) -> str:
        """给下一轮模型构造短状态 JSON，只含白名单元数据，不含命令／日志／原因文本。"""
        with self._lock:
            if not self.jobs:
                return ""
            values = [{"job_id": key, "status": e["record"]["status"],
                       "exit_code": e["record"]["exit_code"], "unobserved": not e["observed"]}
                      for key, e in self.jobs.items()
                      if not e["observed"] or e["record"]["status"] not in TERMINAL]
            payload = {"jobs": values, "archived_count": sum(e["archived"] for e in self.jobs.values()),
                       "control_error": bool(self._fault)}
        return result_json(payload, 2048)

    def execute(self, name: str, arguments: dict[str, Any], maximum: int) -> ToolResult:
        """四个模型工具的分发入口；maximum 是返回 JSON 的字符预算。

        内部先验证参数，再调用 start/get/wait/cancel。预期失败返回工具错误，
        由 Agent 加上真实 call_id；此处空 call_id 只是运行时结果的占位值。
        """
        try:
            allowed = {"command", "timeout_s"} if name == "start_background" else {"job_id", "tail_chars"}
            if name == "cancel_background":
                allowed = {"job_id"}
            if set(arguments) - allowed:
                raise ValueError("unexpected background argument")
            tail_chars = arguments.get("tail_chars", 4000)
            if type(tail_chars) is not int or not 0 <= tail_chars <= 8000:
                raise ValueError("tail_chars must be an integer in 0..8000")
            if name == "start_background":
                command = arguments.get("command")
                if not isinstance(command, str):
                    raise ValueError("command must be a string")
                value = self.start(command, arguments.get("timeout_s", 60))
            elif name == "get_background" and "job_id" not in arguments:
                self.refresh()
                with self._lock:
                    value = {"jobs": [{**self._metadata(e["record"]), "unobserved": not e["observed"]}
                                      for e in self.jobs.values()
                                      if not e["observed"] or e["record"]["status"] not in TERMINAL],
                             "archived_count": sum(e["archived"] for e in self.jobs.values())}
            elif name == "get_background":
                value = self.get(job_id(arguments.get("job_id")), tail_chars)
            elif name == "wait_background":
                value = self.wait(job_id(arguments.get("job_id")), tail_chars)
            elif name == "cancel_background":
                value = self.cancel(job_id(arguments.get("job_id")))
            else:
                raise ValueError("unknown background tool")
            # 状态请求成功也可能读到命令失败；is_error 依最终任务状态判定。
            return ToolResult("", name, result_json(value, maximum), value.get("status") in TERMINAL - {"succeeded"})
        except (SandboxError, ValueError, OSError) as exc:
            return ToolResult("", name, "Error: " + str(exc), True)

    def _empty_archive(self, identifier: str, record: dict[str, Any], *, incomplete: bool) -> None:
        """仅用于已证明没有启动，或容器已停止且日志无法收集的路径。"""
        # incomplete=False 是确定未启动、没有日志；True 是原日志无法可靠取得。
        # 仍写结果记录，不能用空输出伪装成一项执行成功的命令。
        path = self.directory / job_id(identifier)
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve()):
            raise SandboxError("invalid background archive path")
        record = dict(record)
        record.update(output_retained_bytes=0,
                      output_truncated=record["output_total_bytes"] > 0,
                      logs_pruned=True, output_incomplete=incomplete)
        for name in ("output.head", "output.tail"):
            atomic_write(path / name, b"")
        atomic_write(path / "result.json", json.dumps(record, ensure_ascii=False).encode("utf-8"))
        self.jobs[identifier].update(record=record, archived=True, archive_bytes=0,
                                     output_incomplete=incomplete,
                                     pruned=True)
        self._persist()

    def mark_container_stopped(self) -> None:
        """容器已确认停止：保留已有结果，其他项记未知并显式标出日志缺失。"""
        self._stop.set()
        with self._condition:
            for identifier, entry in self.jobs.items():
                if entry["archived"]:
                    continue
                record = dict(entry["record"])
                # 容器停止证明不再写入，所以 cleanup_confirmed=True；它无法证明
                # 原命令如何退出，故 status=lost、exit_code=None。
                record.update(status="lost", reason="container_stopped", exit_code=None,
                              cleanup_confirmed=True)
                self._empty_archive(identifier, record, incomplete=True)
            if self.jobs:
                self._persist()
            self._condition.notify_all()

    def shutdown(self) -> None:
        """正常退出先收集结果再停止容器；归档失败允许重试并保留来源。"""
        with self._lock:
            if self._closed:
                return
            self._closing = True
        # 先停监测，避免关闭流程与监测器竞争控制通道；不在状态锁内 join。
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=12)
            if self._thread.is_alive():
                raise SandboxError("background monitor did not stop")
        with self._lock:
            active = [k for k, e in self.jobs.items() if not e["archived"]]  # 所有仍需收尾／收集的项。
        for identifier in active:
            try:
                self.cancel(identifier)
            except SandboxError:
                # 控制或清理不可靠时确保整个容器停止；普通文件归档失败不进此兜底。
                self.abort()
                with self._lock:
                    if any(not e["archived"] for e in self.jobs.values()):
                        raise
        with self._lock:
            self._closed = True
