"""复制工作区并验证容器导出的 tar；不直接挂载可写宿主目录。"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from study_coder.sandbox.base import SandboxError

MAX_FILES = 10_000
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
# 这些是传输过程本身的上限。策略可以限制工作区总量，但单个导入/导出文件
# 始终受 MAX_FILE_BYTES 约束。排除规则同时用于导入和导出。
_EXCLUDED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                   ".pytest_cache", ".pytest-tmp", ".uv-cache", ".npm", ".study-coder",
                   "sessions", ".ssh", ".aws", ".azure",".cache"}
_EXCLUDED_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*",
                      "credentials.json", "*.pfx", "*.p12")
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excluded(relative: str, extra: tuple[str, ...] = ()) -> bool:
    """判断 POSIX 相对路径是否应跳过；默认保留 .env.example 模板。

    默认规则按每一级路径组件检查，因此被排除的目录及其后代都不会导入。
    extra 既匹配单个组件，也匹配完整相对路径及其父目录；排除目录会排除其后代。
    """
    parts = PurePosixPath(relative).parts
    return (any(part in _EXCLUDED_PARTS for part in parts)
            or any(fnmatch.fnmatch(part, pattern) for part in parts
                   if part.casefold() != ".env.example"
                   for pattern in _EXCLUDED_PATTERNS)
            or any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in extra)
            or any(fnmatch.fnmatch("/".join(parts[:index]), pattern)
                   for index in range(1, len(parts) + 1) for pattern in extra))


def filter_snapshot(snapshot: dict[str, str],
                    extra_exclude: tuple[str, ...] = ()) -> dict[str, str]:
    """过滤导入基线，避免导出时新排除的文件被误判为已删除。"""
    return {path: digest for path, digest in snapshot.items() if not _excluded(path, extra_exclude)}


def tar_exclude_patterns(extra_exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
    """返回与路径组件排除规则一致的 GNU tar --exclude 模式。

    调用 tar 时禁止通配符跨斜杠，让预过滤只排除宿主规则也会排除的路径。
    自定义规则加 ./ 对应根相对成员名；安全的单组件规则还可匹配任意层级。
    ?、*.* 等能匹配根操作数 . 的规则只生成带前缀的版本，避免误排除整个工作区。
    tar 预过滤可以少排除；宿主仍用 _excluded 完整校验并过滤。
    """
    patterns = set(_EXCLUDED_PARTS)
    for pattern in extra_exclude:
        patterns.add("./" + pattern)
        if "/" not in pattern:
            patterns.add("./*/" + pattern if fnmatch.fnmatch(".", pattern) else pattern)
    return tuple(sorted(patterns))


def _reparse(path: Path) -> bool:
    """Windows junction/reparse point 与 POSIX 符号链接都不能跨边界。"""
    info = path.lstat()
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400))


def _read_stable_file(path: Path, relative: str) -> tuple[bytes, os.stat_result]:
    """返回文件字节和打开时的元数据；拒绝读取期间发生变化的文件。

    lstat 检查路径，fstat 检查已打开句柄；前后对照可发现常见的链接替换、
    重命名或并发写入。导入必须得到确定的一份内容及对应哈希。
    """
    before = path.lstat()
    if _reparse(path) or not stat.S_ISREG(before.st_mode):
        raise SandboxError(f"unsupported workspace entry: {relative}")
    if before.st_size > MAX_FILE_BYTES:
        raise SandboxError(f"workspace file or count limit exceeded: {relative}")
    descriptor, opened = _open_stable_file(path, relative)
    try:
        if _file_identity(before) != _file_identity(opened):
            raise SandboxError(f"workspace changed during import: {relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        if (_reparse(path)
                or _changed_on_same_source(opened, after)
                or _file_identity(path_after) != _file_identity(opened)
                or len (data) != opened.st_size):
            raise SandboxError(f"workspace changed during import: {relative}")
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError(f"workspace file or count limit exceeded: {relative}")
        return data, opened
    finally:
        os.close(descriptor)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """提取设备、inode、大小和时间信息，用于比较路径与句柄是否仍指同一版本。"""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

def _changed_on_same_source(before: os.stat_result,
                            after: os.stat_result) -> bool:
    """同一 API 的两次读取（如两次 fstat）之间的变化检查，可安全带上 ctime。"""
    return (_file_identity(before) != _file_identity(after)
            or before.st_ctime_ns != after.st_ctime_ns)

def _open_stable_file(path: Path, relative: str) -> tuple[int, os.stat_result]:
    """只打开普通文件，返回文件描述符及元数据；调用者负责关闭描述符。"""
    before = path.lstat()
    if _reparse(path) or not stat.S_ISREG(before.st_mode):
        raise SandboxError(f"unsupported file: {relative}")
    # O_NOFOLLOW 在支持的平台阻止最终组件被换成符号链接；随后仍需复查路径。
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SandboxError(f"file changed while opening: {relative}") from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (_reparse(path) or not stat.S_ISREG(opened.st_mode)
                or _file_identity(before) != _file_identity(opened)
                or _file_identity(current) != _file_identity(opened)):
            raise SandboxError(f"file changed while opening: {relative}")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _safe_name(name: str) -> str:
    """把 tar 成员名规范成安全相对路径；拒绝 Windows/POSIX 越界写入。

    容器使用 POSIX 路径，但导出目标可能在 Windows：盘符、保留名、尾随点/空格、
    大小写碰撞等都要按宿主目标的规则处理。空根目录成员返回空字符串。
    """
    if not name or name.startswith("/") or "\\" in name or "\0" in name:
        raise SandboxError(f"unsafe archive path: {name!r}")
    raw = name[2:] if name.startswith("./") else name
    raw = raw.rstrip("/")
    if raw in {"", "."}:
        return ""
    parts = raw.split("/")
    if any(part in {"", ".", ".."} or ":" in part or part.endswith((" ", "."))
           or _RESERVED.fullmatch(part) or any(ord(ch) < 32 for ch in part)
           for part in parts):
        raise SandboxError(f"unsafe archive path: {name!r}")
    return PurePosixPath(*parts).as_posix()


def create_import_archive(root: Path, archive: Path,
                          extra_exclude: tuple[str, ...] = (), *,
                          max_total_bytes: int = MAX_TOTAL_BYTES,
                          max_files: int = MAX_FILES) -> dict[str, str]:
    """从宿主工作区生成导入 tar，并返回 {相对路径: SHA-256} 快照。

    输入 root/目标 archive/附加排除规则与预算；副作用是创建 tar。
    遇链接、特殊文件、并发变化或超限即抛 SandboxError，调用者负责清理任务。
    """
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise SandboxError("workspace is not a directory")
    snapshot: dict[str, str] = {}
    total = 0
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w") as output:
        for base, dirs, files in os.walk(root, followlinks=False):
            current = Path(base)
            # 修改 os.walk 原地提供的 dirs，确保排除目录不会继续被递归访问。
            for name in sorted(dirs[:]):
                path = current / name
                relative = path.relative_to(root).as_posix()
                if _excluded(relative, extra_exclude):
                    dirs.remove(name)
                    continue
                if _reparse(path) or not path.is_dir():
                    raise SandboxError(f"unsupported workspace entry: {relative}")
                # tar 只记录简单目录元数据，不携带宿主所有者及额外权限。
                info = tarfile.TarInfo(relative + "/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                output.addfile(info)
            for name in sorted(files):
                path = current / name
                relative = path.relative_to(root).as_posix()
                if _excluded(relative, extra_exclude):
                    continue
                if len(snapshot) >= max_files:
                    raise SandboxError(f"workspace file or count limit exceeded: {relative}")
                data, file_info = _read_stable_file(path, relative)
                size = file_info.st_size
                total += size
                if total > max_total_bytes:
                    raise SandboxError("workspace import exceeds configured workspace limit")
                snapshot[relative] = _hash(data)
                # 哈希取自同一份 data；后续 apply 用它判断宿主原文件是否被改动。
                info = tarfile.TarInfo(relative)
                info.size = size
                info.mode = 0o755 if file_info.st_mode & stat.S_IXUSR else 0o644
                output.addfile(info, io.BytesIO(data))
    return snapshot


def extract_export(stream: BinaryIO, staging: Path,
                   extra_exclude: tuple[str, ...] = (), *,
                   max_total_bytes: int = MAX_TOTAL_BYTES,
                   max_files: int = MAX_FILES) -> dict[str, str]:
    """流式校验容器导出 tar，写入新 staging 目录并返回文件哈希快照。

    输入是二进制 tar 流、目标暂存目录、排除规则和预算；不触碰原工作区。
    先校验成员路径、重复项和归档成员数，再排除无需交付的内容；
    只有保留的文件计入单文件大小、文件数量与总字节配额。
    """
    staging.mkdir(parents=True, exist_ok=False)
    snapshot: dict[str, str] = {}
    seen: set[str] = set()
    count = total = file_count = 0
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            # 流式模式不把整个 tar 载入内存；不能依赖 tarfile 的直接解包函数。
            relative = _safe_name(member.name)
            if not relative:
                continue
            folded = relative.casefold()
            # Windows 上 Foo 与 foo 可能指向同一目标，所以用 casefold 检查重复。
            if folded in seen:
                raise SandboxError(f"duplicate or case-colliding archive path: {relative}")
            seen.add(folded)
            count += 1
            if count > max_files * 2:
                raise SandboxError("export file count limit exceeded")
            # 排除项不写入交付目录，也不占用交付文件配额；所有成员仍受上面的
            # 路径、重复项及成员总数检查约束，避免排除规则绕过归档结构限制。
            if _excluded(relative, extra_exclude):
                continue
            if member.isfile():
                file_count += 1
                if member.size > MAX_FILE_BYTES or file_count > max_files:
                    raise SandboxError(f"export file or count limit exceeded: {relative}")
                total += member.size
                if total > max_total_bytes:
                    raise SandboxError("workspace export exceeds configured workspace limit")
            # 被排除的成员不会写入交付物；例如 node_modules 中的硬链接无需
            # 解引用或导出。未排除的链接和特殊文件仍保持拒绝策略。
            if not member.isdir() and not member.isfile():
                raise SandboxError(f"unsupported export entry: {relative}")
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise SandboxError(f"export file could not be read: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            remaining = member.size
            with destination.open("xb") as output:
                # xb 拒绝覆盖；remaining 保证只读该成员声明的字节数并发现截断。
                while remaining:
                    chunk = source.read(min(65_536, remaining))
                    if not chunk:
                        raise SandboxError(f"truncated export file: {relative}")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            snapshot[relative] = digest.hexdigest()
    return snapshot


def changes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """比较导入/导出快照，返回 added、modified、deleted 三类相对路径。"""
    return {
        "added": sorted(after.keys() - before.keys()),
        "modified": sorted(name for name in before.keys() & after.keys()
                           if before[name] != after[name]),
        "deleted": sorted(before.keys() - after.keys()),
    }


def _check_host_path(root: Path, relative: str) -> Path:
    """应用前检查目标及每级父目录，拒绝链接与工作区外路径。"""
    target = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _reparse(current):
                raise SandboxError(f"host path became a link: {relative}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise SandboxError(f"host path escapes workspace: {relative}")
    return target


def _save_json_atomic(path: Path, value: object) -> None:
    """先持久化同目录临时 JSON，再原子替换应用事务日志。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as output:
            temporary = output.name
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _file_hash(path: Path) -> str | None:
    """返回稳定普通文件的 SHA-256；不存在返回 None，链接/变化会失败。"""
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    descriptor, opened = _open_stable_file(path, path.name)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            while chunk := source.read(65_536):
                digest.update(chunk)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        if (_reparse(path) or _changed_on_same_source(opened, after)
                or _file_identity(path_after) != _file_identity(opened)):
            raise SandboxError(f"file changed while hashing: {path.name}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _copy_stable_file(source_path: Path, destination: Path, relative: str, *,
                      expected_hash: str | None = None,
                      mode_override: int | None = None,
                      fail_if_exists: bool = False) -> tuple[str, int]:
    """稳定复制一个文件，返回内容哈希和原权限位。

    先在目标同目录写临时文件，检查源文件身份与期望哈希，再 os.replace。
    备份和最终写入都复用此函数；异常时关闭句柄并删除临时文件。
    """
    if fail_if_exists and (destination.exists() or destination.is_symlink()):
        raise SandboxError(f"backup already exists: {relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, opened = _open_stable_file(source_path, relative)
    temporary: str | None = None
    try:
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = output.name
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                while chunk := source.read(65_536):
                    output.write(chunk)
                    digest.update(chunk)
            after = os.fstat(descriptor)
            path_after = source_path.lstat()
            if (_reparse(source_path)
                    or _changed_on_same_source(opened, after)
                    or _file_identity(path_after) != _file_identity(opened)):
                raise SandboxError(f"file changed while copying: {relative}")
            file_hash = digest.hexdigest()
            if expected_hash is not None and file_hash != expected_hash:
                raise SandboxError(f"file content changed while copying: {relative}")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode_override if mode_override is not None
                 else stat.S_IMODE(opened.st_mode))
        os.replace(temporary, destination)
        return file_hash, stat.S_IMODE(opened.st_mode)
    finally:
        os.close(descriptor)
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _replace_file(target: Path, source: Path, relative: str, expected_hash: str,
                  mode: int) -> None:
    _copy_stable_file(source, target, relative, expected_hash=expected_hash,
                      mode_override=mode)


def recover_apply(root: Path, journal_path: Path) -> None:
    """按应用日志逆序回滚；返回 None，冲突则抛 SandboxError。

    只有当前内容仍等于本事务写入值（或删除结果）才恢复备份。
    若被第三方再次修改，则记录冲突并保留现场，避免回滚覆盖用户改动。
    """
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        operations = journal["operations"]
        if not isinstance(operations, list):
            raise ValueError("invalid operations")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SandboxError("sandbox apply recovery journal is unavailable") from exc
    root = root.resolve(strict=True)
    backup_root = journal_path.with_suffix("") / "backups"
    conflicts: list[str] = []
    # 逆序撤销可正确处理同一事务中相互依赖的写入；每项都重新验证宿主路径。
    for operation in reversed(operations):
        relative = _safe_name(operation["path"])
        target = _check_host_path(root, relative)
        current_hash = _file_hash(target)
        before_hash = operation.get("before_hash")
        after_hash = operation.get("after_hash")
        if operation.get("had_original"):
            if current_hash == before_hash:
                continue
            if operation.get("action") == "delete" and current_hash is None:
                can_restore = True
            else:
                can_restore = current_hash == after_hash
            if not can_restore:
                conflicts.append(relative)
                continue
            backup = backup_root.joinpath(*PurePosixPath(relative).parts)
            if _file_hash(backup) != before_hash:
                conflicts.append(relative)
                continue
            mode = operation.get("mode", 0o644)
            if type(mode) is not int:
                conflicts.append(relative)
                continue
            _replace_file(target, backup, relative, before_hash, mode)
        elif current_hash == after_hash:
            target.unlink()
        elif current_hash is not None:
            conflicts.append(relative)
    journal["status"] = "recovery_required" if conflicts else "rolled_back"
    journal["conflicts"] = conflicts
    _save_json_atomic(journal_path, journal)
    if conflicts:
        raise SandboxError("sandbox apply recovery found concurrent changes: "
                           + ", ".join(conflicts[:10]))


def apply_changes(root: Path, staging: Path, before: dict[str, str],
                  after: dict[str, str], journal_path: Path | None = None) -> dict[str, list[str]]:
    """把已校验导出差异应用到原工作区，返回与 changes 相同的差异表。

    输入 before/after 是导入与导出的哈希快照，staging 是容器导出副本。
    先备份并验证所有源/目标，写 prepared 日志，再逐项原子替换或删除。
    失败时按持久化日志回滚；并发修改会触发冲突而不会被静默覆盖。
    """
    root = root.resolve(strict=True)
    diff = changes(before, after)
    journal_path = journal_path or staging.parent / f"apply-{uuid.uuid4().hex}.json"
    backup_root = journal_path.with_suffix("") / "backups"
    originals: dict[str, tuple[Path, int] | None] = {}
    writes: dict[str, Path] = {}
    operations: list[dict[str, object]] = []
    journal: dict[str, object] = {"status": "prepared", "operations": operations}
    prepared = False
    transaction_dir = journal_path.with_suffix("")
    try:
        # 准备阶段不修改原工作区：先检查所有目标仍与导入快照一致并保存备份。
        for relative in (*diff["added"], *diff["modified"], *diff["deleted"]):
            _safe_name(relative)
            target = _check_host_path(root, relative)
            expected = before.get(relative)
            if _file_hash(target) != expected:
                raise SandboxError(f"host file changed during sandbox task: {relative}")
            if expected is None:
                originals[relative] = None
            else:
                backup = backup_root.joinpath(*PurePosixPath(relative).parts)
                _, mode = _copy_stable_file(target, backup, relative,
                                            expected_hash=expected, mode_override=0o600,
                                            fail_if_exists=True)
                originals[relative] = (backup, mode)
        for relative in (*diff["added"], *diff["modified"]):
            # 暂存目录也可能被其他进程改变；应用前重新核对导出哈希。
            source = staging.joinpath(*PurePosixPath(relative).parts)
            if _file_hash(source) != after[relative]:
                raise SandboxError(f"staged export changed: {relative}")
            writes[relative] = source
        for relative in (*diff["added"], *diff["modified"], *diff["deleted"]):
            prior = originals.get(relative)
            had_original = relative in before
            operation: dict[str, object] = {
                "path": relative,
                "action": "delete" if relative in diff["deleted"] else "write",
                "had_original": had_original,
                "before_hash": before.get(relative),
                "after_hash": after.get(relative),
                "mode": prior[1] if prior is not None else 0o644,
            }
            if had_original:
                operation["backup"] = relative
            operations.append(operation)
        _save_json_atomic(journal_path, journal)
        prepared = True
    except Exception:
        if not prepared:
            shutil.rmtree(transaction_dir, ignore_errors=True)
        raise
    try:
        # 日志先变为 applying，再改宿主文件；中途崩溃可由 recover_task 接续回滚。
        journal["status"] = "applying"
        _save_json_atomic(journal_path, journal)
        for operation in operations:
            relative = str(operation["path"])
            target = _check_host_path(root, relative)
            if _file_hash(target) != operation["before_hash"]:
                raise SandboxError(f"host file changed during sandbox apply: {relative}")
            if operation["action"] == "delete":
                target.unlink()
            else:
                _check_host_path(root, relative)
                write_mode = operation["mode"]
                if type(write_mode) is not int:
                    raise SandboxError(f"apply journal has an invalid mode: {relative}")
                _replace_file(target, writes[relative], relative,
                              str(operation["after_hash"]), write_mode or 0o644)
            operation["applied"] = True
            # 每个操作落盘后更新日志，记录可供下一次进程恢复使用。
            _save_json_atomic(journal_path, journal)
        journal["status"] = "applied"
        _save_json_atomic(journal_path, journal)
    except Exception as exc:
        try:
            recover_apply(root, journal_path)
        except Exception as rollback_error:
            if not isinstance(rollback_error, SandboxError):
                rollback_error = SandboxError(f"rollback failed: {rollback_error}")
            raise SandboxError(f"sandbox apply failed ({exc}); {rollback_error}") from exc
        if isinstance(exc, SandboxError):
            raise
        raise SandboxError(f"sandbox apply failed and was rolled back: {exc}") from exc
    return diff
