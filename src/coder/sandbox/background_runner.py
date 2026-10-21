"""容器内后台命令和短控制入口；控制记录不进入 /workspace 导出。

--control 读取一次 JSON 请求并退出；--run 持有一个命令直到清理结束。
只在控制入口使用 Linux flock，模块可以在 Windows 宿主导入做协议校验。

进程关系：宿主 docker exec -> 短控制进程 -> 独立 runner -> /bin/sh -> 命令后代。
控制进程返回后，runner 继续存活；runner 最终清理后代并提交 status.json。
每项任务使用 ROOT/job_id/ 下的 status.json、output.head、output.tail，取消请求
使用 cancel.request 标记。宿主随后复制结果归档，这些内部文件不属于项目源码。

record（状态字典）的重要字段：
    job_id：一次后台执行的编号，不是模型工具调用的 call_id，也不是 .tasks 的编号。
    version：状态记录格式版本；command/timeout_s 是登记后不能改变的执行意图。
    status：starting/running 表示尚未结束，其余状态属于 TERMINAL。
    created_at：登记时的 Unix 时间，单位秒；用于跨进程估算任务年龄。
    elapsed_s：runner 使用单调时钟测得的耗时，单位秒；最终值包含清理时间。
    reason：机器可读的结束原因；exit_code 是 shell 的实际退出码，未知时为 None。
    runner_pid：持有命令的 Python runner PID，区别于 shell PID。
    runner_start：/proc/PID/stat 的启动时钟计数，按字符串保存，用于识别 PID 复用。
    cleanup_confirmed：是否确认受管理后代停止；终态本身不代表清理已经完成。
    output_limit：单任务头部与尾部日志合计上限，单位字节。
    output_total_bytes：累计读到的全部输出字节；output_retained_bytes：实际保留字节。
    output_truncated：累计输出超过保留部分；logs_pruned：容器日志已回收。
    cancel_requested：runner 已处理取消信号／标记；不是“已经清理完成”。
    workspace_max_bytes/workspace_max_files：共享工作区的扫描阈值，不是任务独享配额。
"""

from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from coder.sandbox.service_runner import _kill_descendants, _workspace_usage
from coder.sandbox.transfer import runtime_export_issues

# ROOT 是容器内部控制目录；放在 /tmp，避免被 /workspace 的导出流程带回项目。
ROOT = Path("/tmp/coder-background")
# lost 表示无法可靠确定最终执行结果；不能把它当作普通非零退出。
TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled", "lost"})
MAX_RECORDS = 32  # 一次沙箱生命周期累计的记录数，包括已结束项；不是并发数。
LOG_BUDGET = 16 * 1024**2  # 容器中未回收日志的预留总预算，单位字节。
ARCHIVE_BUDGET = 64 * 1024**2  # 宿主归档总预算；宿主管理器导入此常量执行检查。
STATUS_LIMIT = 65536  # 一份状态 JSON 的读取上限，单位字节。


class StartRejected(ValueError):
    """只用于创建任务目录之前的确定性拒绝；此时命令没有启动。"""


def job_id(value: Any) -> str:
    """校验并原样返回任务编号；固定格式防止编号被用作任意文件路径。"""
    if not isinstance(value, str) or not re.fullmatch(r"job_[0-9a-f]{32}", value):
        raise ValueError("invalid background job_id")
    return value


def atomic_write(path: Path, data: bytes) -> None:
    """把 data 字节写入 path；先落盘临时文件，再替换单个目标文件。

    原子替换让并发读取者看到旧文件或新文件，而不是写到一半的 JSON。
    这只保证单个文件替换，不是多个状态／日志文件的一次事务。
    """
    # temporary 与目标同目录，以便 os.replace 在同一文件系统中原子替换。
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            # xb 是排他创建；随机临时路径已存在时失败，而不是覆盖它。
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def regular_bytes(path: Path, maximum: int) -> bytes:
    """读取普通文件并返回 bytes；maximum 是字节上限，违规抛出 ValueError。

    状态和日志可能被命令改写，不能只相信路径先前的类型或大小。
    """
    if path.is_symlink():
        raise ValueError("background file must not be a symlink")
    # fd 是已打开文件的描述符。NOFOLLOW 拒绝末端符号链接；NONBLOCK 防止
    # 路径被换成 FIFO 后卡在 open；BINARY 适配宿主 Windows 的二进制读取。
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        # info 来自已打开的句柄，校验实际读取对象，而不是重新查询路径。
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("invalid background file or file size")
        # 多读一个字节，用来发现 fstat 之后增长的文件；仍保持有界读取。
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("background file exceeds size limit")
        return data


def validate_status(value: Any) -> dict[str, Any]:
    """校验状态协议并返回原字典；不复制、不修正字段，异常统一拒绝。

    容器和宿主共用校验；有效状态仍不能证明同 UID 命令没有篡改它。
    """
    # bool 是 int 的子类，使用 type(...) is int 防止 True 被当作版本或秒数。
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("invalid background status version")
    job_id(value.get("job_id"))
    if value.get("status") not in TERMINAL | {"starting", "running"}:
        raise ValueError("invalid background state")
    for field in ("command", "reason"):
        if not isinstance(value.get(field), str) or len(value[field]) > 16384:
            raise ValueError("invalid background text field")
    for field in ("timeout_s", "output_limit", "output_total_bytes", "output_retained_bytes"):
        if type(value.get(field)) is not int or not 0 <= value[field] <= 2**63 - 1:
            raise ValueError("invalid background numeric field")
    if not 1 <= value["timeout_s"] <= 86400 or not 4096 <= value["output_limit"] <= 8388608:
        raise ValueError("invalid background resource limit")
    if value["output_retained_bytes"] > value["output_limit"]:
        raise ValueError("background output budget exceeded")
    for field in ("created_at", "elapsed_s"):
        # NaN/Infinity 无法用于可靠比较截止时间，必须同时校验有限性。
        if type(value.get(field)) not in (int, float) or not math.isfinite(value[field]) or value[field] < 0:
            raise ValueError("invalid background time")
    for field in ("cleanup_confirmed", "logs_pruned", "cancel_requested", "output_truncated"):
        if type(value.get(field)) is not bool:
            raise ValueError("invalid background boolean")
    for field in ("runner_pid", "exit_code"):
        if value.get(field) is not None and (type(value[field]) is not int
                                            or abs(value[field]) > 2**31 - 1):
            raise ValueError("invalid background process field")
    if value.get("runner_pid") is not None and value["runner_pid"] <= 1:
        raise ValueError("background runner_pid must be greater than one")
    start = value.get("runner_start")
    # start 是进程启动计数的字符串，不是 created_at 的 Unix 时间戳。
    if start is not None and (not isinstance(start, str) or not re.fullmatch(r"\d{1,24}", start)):
        raise ValueError("invalid background process identity")
    return value


def _directory(identifier: str) -> Path:
    """返回容器内该任务目录，并拒绝符号链接和越过 ROOT 的解析结果。"""
    path = ROOT / job_id(identifier)
    if ROOT.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("invalid background directory")
    return path


def _load(identifier: str) -> dict[str, Any]:
    """有界读取 status.json，校验内容编号与目录编号一致后返回状态字典。"""
    value = validate_status(json.loads(regular_bytes(_directory(identifier) / "status.json",
                                                    STATUS_LIMIT)))
    if value["job_id"] != identifier:
        raise ValueError("background record ownership mismatch")
    return value


def _save(value: dict[str, Any]) -> None:
    """验证状态后原子写入对应任务的 status.json；不改变传入字典。"""
    atomic_write(_directory(value["job_id"]) / "status.json",
                 json.dumps(validate_status(value), ensure_ascii=False).encode("utf-8"))


def _identity(pid: int) -> tuple[int, str, str] | None:
    """返回 (进程组 PGID, 启动计数, 进程状态)，读取失败则返回 None。

    PID 可能复用；只有进程组和启动计数仍一致，才可对原任务发信号。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # comm 字段被括号包围且可能含空格，先越过最后一个右括号再 split。
        # fields[0]/[2]/[19] 对应原 stat 的第 3/5/22 项：状态、PGID、starttime。
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[2]), fields[19], fields[0]
    except (OSError, ValueError, IndexError):
        return None


def _reconcile(record: dict[str, Any]) -> dict[str, Any]:
    """进程身份丢失只能报告未知；不要从旧 running 记录推断成功。"""
    if record["status"] in TERMINAL:
        return record
    pid = record["runner_pid"]
    identity = _identity(pid) if pid else None
    # runner 以新 session 启动，所以其 PGID 应等于 PID；Z/X 已不能继续执行。
    alive = identity and identity[:2] == (pid, record["runner_start"]) and identity[2] not in {"Z", "X"}
    # 登记 starting 到 runner 写 PID 存在短窗口，给无 PID 的新记录 10 秒宽限。
    if alive or (pid is None and time.time() - record["created_at"] < 10):
        return record
    # 查询身份时 runner 可能恰好提交最终状态；重读避免用旧 running 覆盖终态。
    latest = _load(record["job_id"])
    if latest["status"] in TERMINAL:
        return latest
    latest.update(status="lost", reason="runner_lost", cleanup_confirmed=False)
    _save(latest)
    return latest


@contextmanager
def _registry_lock() -> Iterator[None]:
    """串行化容器控制进程的名额检查与任务登记；退出上下文时释放 flock。

    这是进程间文件锁，区别于宿主 BackgroundManager 内部的线程锁。
    """
    import fcntl  # 容器是 Linux；宿主只导入本模块的协议和文件辅助函数。

    ROOT.mkdir(mode=0o700, exist_ok=True)
    if ROOT.is_symlink():
        raise ValueError("invalid background registry")
    fd = os.open(ROOT / "registry.lock", os.O_CREAT | os.O_RDWR
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid background registry lock")
        getattr(fcntl, "flock")(fd, getattr(fcntl, "LOCK_EX"))
        # yield 期间持有注册锁；关闭文件描述符会释放该锁，包括异常退出。
        yield
    finally:
        os.close(fd)


def _records() -> list[dict[str, Any]]:
    """枚举有合法编号的记录，并把丢失 runner 的旧活动状态纠正为 lost。"""
    if not ROOT.exists():
        return []
    if ROOT.is_symlink():
        raise ValueError("invalid background registry")
    identifiers = [path.name for path in ROOT.iterdir()
                   if re.fullmatch(r"job_[0-9a-f]{32}", path.name)]
    if len(identifiers) > MAX_RECORDS:
        raise ValueError("background record limit exceeded")
    return [_reconcile(_load(identifier)) for identifier in identifiers]


def _start(request: dict[str, Any]) -> dict[str, Any]:
    """登记启动意图，创建独立 runner，返回当前状态而不是等命令执行结束。

    request 含 command/timeout_s 以及宿主加入的预算；返回值是状态 record。
    目录创建前的拒绝可证明没有启动；之后的通信故障必须按结果未知处理。
    """
    identifier = job_id(request.get("job_id"))
    command = request.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 16384 or "\0" in command:
        raise StartRejected("command must be nonempty text of at most 16384 characters")
    for field, minimum, maximum in (("timeout_s", 1, 86400), ("max_jobs", 1, 16),
                                    ("output_limit", 4096, 8388608),
                                    ("workspace_max_bytes", 128 * 1024**2, 64 * 1024**3),
                                    ("workspace_max_files", 10000, 1000000)):
        if type(request.get(field)) is not int or not minimum <= request[field] <= maximum:
            raise StartRejected(f"invalid background {field}")
    with _registry_lock():
        # 名额检查和 starting 登记在同一把进程锁中，防止两个启动抢到最后一格。
        if _directory(identifier).exists():
            return _load(identifier)  # 同一请求重传，不重新执行命令。
        records = _records()
        if len(records) >= MAX_RECORDS:
            raise StartRejected("background lifetime record limit reached")
        # active 包含未结束项和清理未确认项；后一类仍可能有进程占资源／写文件。
        active = [r for r in records if r["status"] not in TERMINAL or not r["cleanup_confirmed"]]
        if len(active) >= request["max_jobs"]:
            raise StartRejected("background concurrency limit reached; wait or cancel a job")
        # reserved 按每项最大日志容量预留，不按当前日志大小；任务后续输出仍有空间。
        reserved = sum(r["output_limit"] for r in records if not r["logs_pruned"])
        if reserved + request["output_limit"] > LOG_BUDGET:
            raise StartRejected("background log budget is full")
        _directory(identifier).mkdir(mode=0o700)
        # record 同时保存不可变执行意图与可变运行状态，字段释义见模块开头。
        record = {**{k: request[k] for k in ("job_id", "command", "timeout_s", "output_limit",
                                              "workspace_max_bytes", "workspace_max_files")},
                  "version": 1, "status": "starting", "created_at": time.time(),
                  "elapsed_s": 0.0, "reason": "", "runner_pid": None, "runner_start": None,
                  "exit_code": None, "cleanup_confirmed": False, "logs_pruned": False,
                  "cancel_requested": False, "output_truncated": False,
                  "output_total_bytes": 0, "output_retained_bytes": 0}
        _save(record)
        try:
            # -I 排除工作区同名包／PYTHONPATH 的影响，-B 避免写入 .pyc。
            # runner 的标准流无需连回控制进程；命令输出由 runner 自己落盘。
            # start_new_session 让 runner 有独立 PGID，控制进程退出后它仍可运行。
            subprocess.Popen([sys.executable, "-I", "-B", "-m",
                              "coder.sandbox.background_runner", "--run", identifier],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError:
            record.update(status="failed", reason="runner_start_failed", cleanup_confirmed=True)
            _save(record)
            return record
    # 不在注册锁里等握手，其他任务可以登记；starting 已占用名额。
    deadline = time.monotonic() + 3  # runner 登记 PID 的握手截止点，不是命令截止点。
    while time.monotonic() < deadline:
        record = _load(identifier)
        if record["runner_pid"] is not None or record["status"] in TERMINAL:
            return record
        time.sleep(0.05)
    return record  # 启动意图已持久化；不能因握手慢而重启同一命令。


def control(request: dict[str, Any]) -> dict[str, Any]:
    """执行一次短控制请求，返回可序列化字典；不等待命令生命周期结束。

    op 是宿主管理器生成的内部操作名，模型只看到四个后台工具。
    返回 record/records 或 base64 日志；PID 和目录由受校验的记录确定。
    """
    operation = request.get("op")
    if operation == "start":
        return {"record": _start(request)}
    if operation == "poll":
        return {"records": _records()}
    identifier = job_id(request.get("job_id"))
    record = _reconcile(_load(identifier))
    if operation == "cancel":
        if record["status"] not in TERMINAL:
            # 先留取消标记：即使 runner 还未写 PID，它启动后也能发现请求。
            atomic_write(_directory(identifier) / "cancel.request", b"cancel\n")
            identity = _identity(record["runner_pid"]) if record["runner_pid"] else None
            if identity and identity[:2] == (record["runner_pid"], record["runner_start"]):
                try:
                    # 信号只发给已验证的 runner，由它清理自己的命令树；不杀容器。
                    os.kill(record["runner_pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        # 这里只确认取消已请求；清理后的终态由 runner 提交，宿主再查询确认。
        return {"record": record}
    if operation == "prune":
        # 宿主已保存并交付的日志可回收；保留 status.json 便于幂等查询和去重。
        with _registry_lock():
            record = _load(identifier)
            if record["status"] not in TERMINAL or not record["cleanup_confirmed"]:
                raise ValueError("cannot prune an active background job")
            for name in ("output.head", "output.tail"):
                (_directory(identifier) / name).unlink(missing_ok=True)
            record["logs_pruned"] = True
            _save(record)
        return {"record": record}
    if operation == "read":
        data = {}  # head/tail 对应 base64 ASCII 字符串，便于二进制日志通过 JSON。
        total = 0  # 本次读取的保留日志字节数，不是记录中的累计输出字节数。
        for name in ("head", "tail"):
            path = _directory(identifier) / ("output." + name)
            raw = regular_bytes(path, record["output_limit"]) if path.exists() or path.is_symlink() else b""
            total += len(raw)
            if total > record["output_limit"]:
                raise ValueError("background log exceeds reserved budget")
            data[name] = base64.b64encode(raw).decode("ascii")
        # 活动任务的日志和状态分次落盘，可以短暂不一致；最终状态提交后则必须匹配。
        if record["status"] in TERMINAL and not record["logs_pruned"] and total != record["output_retained_bytes"]:
            raise ValueError("terminal background logs are incomplete")
        return {"record": record, **data}
    raise ValueError("unknown background control operation")


class BoundedLog:
    """合并 stdout/stderr 的有界缓冲：1/4 容量留开头，3/4 留滚动末尾。

    读取线程 append，runner 主线程 snapshot，因此缓冲与计数需要同一把锁。
    超限只丢弃旧内容，继续读管道，避免命令因写满 stdout 而阻塞。
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit  # head + tail 的最大保留字节数。
        self.head = bytearray()  # 最早输出，填满 limit//4 后不再改变。
        self.tail = bytearray()  # 未进入 head 的输出末尾，超过容量时丢弃最旧字节。
        self.total = 0  # 累计读取字节数，包含已经丢弃的部分。
        self.lock = threading.Lock()  # 保证 snapshot 三项来自同一个更新边界。

    def append(self, data: bytes) -> None:
        """加入本次读到的字节；头尾不重叠，总保留容量始终不超过 limit。"""
        with self.lock:
            self.total += len(data)
            count = min(len(data), self.limit // 4 - len(self.head))  # 本块可填入头部的字节数。
            self.head.extend(data[:count])
            self.tail.extend(data[count:])
            excess = len(self.tail) - (self.limit - self.limit // 4)  # 尾部超出的字节数。
            if excess > 0:
                del self.tail[:excess]

    def snapshot(self) -> tuple[bytes, bytes, int]:
        """返回不可变 (头部字节, 尾部字节, 累计输出字节数) 的一致快照。"""
        with self.lock:
            return bytes(self.head), bytes(self.tail), self.total


def _run(identifier: str) -> int:
    """运行一条已登记命令，直到退出／超时／取消，并提交清理后的最终状态。

    identifier 用于读取执行意图；最终结果写文件。函数返回的是 runner 自身
    的控制退出码，命令退出码另存于 record['exit_code']，二者不能混用。
    """
    record = _load(identifier)
    directory = _directory(identifier)
    started = time.monotonic()  # 本进程耗时起点；成功创建命令后重新开始计时。
    process: subprocess.Popen[bytes] | None = None  # shell 子进程，不是当前 runner。
    reader: threading.Thread | None = None  # 专门排空合并输出管道的线程。
    log = BoundedLog(record["output_limit"])  # 内存中有限的原始字节，不假设 UTF-8。
    stop = threading.Event()  # SIGTERM 设置的取消意图；清理由主循环按顺序完成。
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    reason = "runner_error"  # 保守默认原因；只有确认具体结束条件后才替换。
    status = "failed"  # 待提交的任务状态；尚未确认清理时不能据此宣布成功。
    read_errors: list[str] = []  # 读取线程反馈的输出失败，主线程据此阻止误报成功。

    def drain(pipe: Any) -> None:
        """连续读取命令 stdout；stderr 已重定向到同一管道。"""
        try:
            while data := pipe.read1(65536):
                log.append(data)
        except OSError:
            read_errors.append("output_read_failed")
        finally:
            pipe.close()

    def flush() -> None:
        """把缓冲头尾分别落盘，并更新 record 计数；调用方随后保存状态。"""
        head, tail, total = log.snapshot()
        atomic_write(directory / "output.head", head)
        atomic_write(directory / "output.tail", tail)
        record.update(output_total_bytes=total, output_retained_bytes=len(head) + len(tail),
                      output_truncated=total > len(head) + len(tail),
                      elapsed_s=round(time.monotonic() - started, 3))

    try:
        # 另建 session 的孙进程也应在父命令退出后归 runner 回收。
        # Linux prctl 的 36 是 PR_SET_CHILD_SUBREAPER。孤儿后代优先被当前 runner
        # 收养，使另建 session 的后代也能按父子关系找到，而不是只靠进程组。
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError("cannot enable child subreaper")
        identity = _identity(os.getpid())
        if identity is None or identity[0] != os.getpid():
            raise OSError("runner has no independent process group")
        record.update(runner_pid=os.getpid(), runner_start=identity[1])
        if (directory / "cancel.request").exists():
            status, reason = "cancelled", "cancel_requested"
        else:
            # shell 解释管道、重定向等语法；cwd 固定在容器 /workspace。
            # stdin=DEVNULL 不提供交互输入，start_new_session 隔离命令进程组。
            process = subprocess.Popen(["/bin/sh", "-c", record["command"]], cwd="/workspace",
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       close_fds=True)
            started = time.monotonic()
            command_identity = _identity(process.pid)  # shell 的 PGID/启动计数/状态快照。
            assert process.stdout is not None
            reader = threading.Thread(target=drain, args=(process.stdout,), daemon=True)
            reader.start()
            record["status"] = "running"
            _save(record)
            last_flush = 0.0  # 上次落盘和配额扫描的单调时间，0 表示应立即首次检查。
            while True:
                # 先检查真实退出，避免已完成命令被稍晚到达的取消改成 cancelled。
                code = process.poll()
                if code is not None:
                    status, reason = ("succeeded", "exit_zero") if code == 0 else ("failed", "nonzero_exit")
                    break
                if stop.is_set() or (directory / "cancel.request").exists():
                    status, reason = "cancelled", "cancel_requested"
                    record["cancel_requested"] = True
                    break
                if time.monotonic() - started >= record["timeout_s"]:
                    status, reason = "timed_out", "command_timeout"
                    break
                if read_errors:
                    raise OSError(read_errors[0])
                if time.monotonic() - last_flush >= 0.5:
                    # size/files 是整个共享工作区当时的扫描值；其他任务也可能在写。
                    size, files = _workspace_usage(Path("/workspace"), record["workspace_max_bytes"],
                                                   record["workspace_max_files"])
                    if size > record["workspace_max_bytes"] or files > record["workspace_max_files"]:
                        status, reason = "failed", "workspace_quota_exceeded"
                        break
                    flush()
                    _save(record)
                    last_flush = time.monotonic()
                time.sleep(0.05)
            if process.poll() is None:
                current = _identity(process.pid)
                # 仍是原 shell 时，先给其进程组 SIGTERM 和短暂的自行退出机会。
                if current and command_identity and current[:2] == command_identity[:2]:
                    getattr(os, "killpg")(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # finally 按后代关系处理另建进程组及残留进程。
    except (OSError, ValueError, subprocess.SubprocessError):
        status, reason = "failed", "runner_error"
    finally:
        # 正常退出也清理残留后代，避免命令自行加 & 后把写入者留在工作区。
        clean = _kill_descendants()  # 是否确认当前 runner 的后代已全部停止／回收。
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                clean = False
        if reader is not None:
            # 管道写端全部关闭后 reader 应退出；它不退出就不能确认输出已收齐。
            reader.join(timeout=2)
            clean = clean and not reader.is_alive()
        if read_errors:
            status, reason = "failed", "output_read_failed"
        try:
            # 先清理后代再检查最终文件，避免快速退出的命令绕过运行中扫描。
            if status == "succeeded":
                issues = runtime_export_issues(Path("/workspace"), record["workspace_max_bytes"],
                                               record["workspace_max_files"])
                if issues:
                    status, reason = "failed", "workspace_unexportable: " + issues[0]
                    log.append(("\n[export preflight failed]\n" + "\n".join(issues)).encode("utf-8"))
            # 顺序是：确认清理 -> 等输出读取结束 -> 最终日志 -> 最终状态。
            flush()
            record.update(status=status if clean else "lost", reason=reason if clean else "cleanup_failed",
                          exit_code=process.returncode if process is not None else None,
                          cleanup_confirmed=clean)
            _save(record)
        except (OSError, ValueError):
            return 2  # 后续查询通过进程身份发现 lost，而不是伪造成功。
    return 0


def main() -> int:
    """--run 进入长期 runner；其他控制调用从 stdin 读取一份有界 JSON。"""
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        return _run(job_id(sys.argv[2]))
    try:
        raw = sys.stdin.buffer.read(65537)  # 请求字节；多读 1 字节检测超过 64 KiB。
        if len(raw) > 65536:
            raise ValueError("background request exceeds 64 KiB")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("background request must be an object")
        response = control(request)
    except (ValueError, OSError) as exc:
        # rejected=True 是“确定没启动”，普通 error 可能发生在登记／启动之后。
        # 宿主据此区分可释放名额的拒绝与必须保留编号追查的未知结果。
        response = {"error": str(exc)[:500], "rejected": isinstance(exc, StartRejected)}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
