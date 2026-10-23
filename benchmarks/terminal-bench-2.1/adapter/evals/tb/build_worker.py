"""把容器内 Worker 打成 zipapp，供任务镜像直接运行。

任务镜像不可控，不能假设里面装了 coder。归档内容只有运行 Worker 所需的
部分：``coder`` 包、``python-dotenv``、``PyYAML``，以及一个
``__main__.py``。``coder/tools/__init__.py`` 在归档里被替换成空壳，
避免 Hands 侧才会用到的 ``tavily`` 被容器导入。

构建结果放在 ``.cache/evals/worker.pyz``；源码更新后按 mtime 自动重建。
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import tempfile
import time
import zipapp
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[5]
SOURCE_ROOT = PROJECT_ROOT / "src"
DEFAULT_ARCHIVE = PROJECT_ROOT / ".cache" / "evals" / "worker.pyz"
# 版本控制里的缓存不参与构建；每次构建都从源码目录复制。
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo",
                                 "*.pyd", "*.so", "*.dll", "*.dylib")
# /workspace 是任务目录的符号链接；先解析成真实路径，文件工具的
# path.relative_to(ctx.workspace) 才不会把 /app/... 和 /workspace 比较。
# TB 的 verifier 在 agent 结束后访问常驻服务，因此评测容器要保留
# bash 派生的后台进程；该开关只由 HarborRuntime 设置。
_MAIN = (
    "import os\n"
    "from pathlib import Path\n\n"
    "from coder.sandbox import worker\n\n"
    "worker.WORKSPACE = Path(worker.WORKSPACE).resolve()\n"
    "if os.environ.get(\"CODER_KEEP_BACKGROUND\") == \"1\":\n"
    "    worker._stop_command_descendants = lambda: None\n\n"
    "raise SystemExit(worker.main())\n"
)
# 容器内只需要 tools 的子模块；包初始化器会连带导入 web_search（tavily）。
_TOOLS_INIT = '"""zipapp 精简入口：容器不加载宿主工具注册表。"""\n'
_DEPENDENCIES = ("dotenv", "yaml")
_LOCK_TIMEOUT_S = 120.0
_STALE_LOCK_S = 600.0


def _package_dir(name: str) -> Path:
    """按当前解释器定位第三方纯 Python 包目录。"""
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(f"worker dependency {name!r} is not installed")
    return Path(next(iter(spec.submodule_search_locations)))


def _source_files() -> list[Path]:
    """构建输入；用于判断缓存是否过期。"""
    roots = [SOURCE_ROOT / "coder", *(_package_dir(name) for name in _DEPENDENCIES)]
    files: list[Path] = []
    for root in roots:
        files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    files.append(Path(__file__))
    return files


def _fresh(archive: Path) -> bool:
    """归档比所有输入都新才复用。"""
    try:
        built = archive.stat().st_mtime
    except OSError:
        return False
    newest = max((path.stat().st_mtime for path in _source_files()), default=built)
    return built >= newest


@contextlib.contextmanager
def _build_lock(archive: Path) -> Iterator[None]:
    """跨进程串行构建：Harbor 可能同时启动多个 trial。"""
    lock = archive.with_name(archive.name + ".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > _STALE_LOCK_S
            except FileNotFoundError:
                continue
            if stale:
                lock.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise RuntimeError(f"timed out waiting for worker build lock: {lock}")
            time.sleep(0.2)
        else:
            try:
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            finally:
                os.close(descriptor)
            break
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def build_worker_zip(destination: Path | str | None = None, *, force: bool = False) -> Path:
    """返回可执行的 ``worker.pyz``；存在有效缓存时不重复打包。"""
    archive = Path(destination) if destination is not None else DEFAULT_ARCHIVE
    archive.parent.mkdir(parents=True, exist_ok=True)
    with _build_lock(archive):
        if not force and _fresh(archive):
            return archive
        staging = Path(tempfile.mkdtemp(prefix="worker-", dir=archive.parent))
        try:
            source = staging / "app"
            source.mkdir()
            shutil.copytree(SOURCE_ROOT / "coder", source / "coder",
                            ignore=_IGNORE)
            for name in _DEPENDENCIES:
                shutil.copytree(_package_dir(name), source / name, ignore=_IGNORE)
            (source / "coder" / "tools" / "__init__.py").write_text(
                _TOOLS_INIT, encoding="utf-8")
            (source / "__main__.py").write_text(_MAIN, encoding="utf-8")
            built = staging / archive.name
            zipapp.create_archive(source, target=built, compressed=True)
            os.replace(built, archive)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return archive
