"""无需 Docker 的策略、导入导出与 Agent 路由测试。"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from coder.agent import Agent
from coder.cli import main
from coder.config import Settings
from coder.llm import ScriptedLLM
from coder.permissions import DefaultPermissionPolicy
from coder.sandbox import worker
from coder.sandbox.base import SandboxError
from coder.sandbox.docker import _format_export_failure, doctor
from coder.sandbox.policy import SandboxPolicy, load_policy
from coder.sandbox.transfer import (
    apply_changes,
    changes,
    create_import_archive,
    extract_export,
    tar_exclude_patterns,
)
from coder.tools import default_tools
from coder.types import ToolCall, ToolResult


def _archive(entries: list[tuple[str, bytes | None, bytes]]) -> io.BytesIO:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if data is not None:
                member.size = len(data)
            output.addfile(member, io.BytesIO(data) if data is not None else None)
    stream.seek(0)
    return stream


def test_import_excludes_credentials_and_export_applies_reviewed_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.py").write_text("before", encoding="utf-8")
    (root / ".env").write_text("FAKE_SECRET=canary", encoding="utf-8")
    (root / ".env.example").write_text("FAKE_SECRET=\n", encoding="utf-8")
    (root / "notes.pem").write_text("fake-key", encoding="utf-8")
    archive = tmp_path / "import.tar"

    before = create_import_archive(root, archive)

    assert list(before) == [".env.example", "main.py"]
    with tarfile.open(archive) as imported:
        assert [item.name for item in imported] == [".env.example", "main.py"]

    exported = _archive([("./main.py", b"after", tarfile.REGTYPE),
                         ("./.env", b"FAKE_SECRET=container", tarfile.REGTYPE),
                         ("./.env.example", b"FAKE_SECRET=placeholder\n", tarfile.REGTYPE),
                         ("./new.txt", b"hello", tarfile.REGTYPE)])
    staging = tmp_path / "export"
    after = extract_export(exported, staging)
    assert changes(before, after) == {"added": ["new.txt"],
                                      "modified": [".env.example", "main.py"], "deleted": []}
    assert not (staging / ".env").exists()
    apply_changes(root, staging, before, after)
    assert (root / "main.py").read_text(encoding="utf-8") == "after"
    assert (root / "new.txt").read_text(encoding="utf-8") == "hello"
    assert (root / ".env").read_text(encoding="utf-8") == "FAKE_SECRET=canary"
    assert (root / ".env.example").read_text(encoding="utf-8") == "FAKE_SECRET=placeholder\n"


def test_user_exclude_can_still_skip_env_example(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".env.example").write_text("FAKE_SECRET=\n", encoding="utf-8")
    assert create_import_archive(root, tmp_path / "import.tar", (".env.example",)) == {}
    exported = _archive([("./.env.example", b"FAKE_SECRET=placeholder\n", tarfile.REGTYPE)])
    assert extract_export(exported, tmp_path / "export", (".env.example",)) == {}
    assert not (tmp_path / "export" / ".env.example").exists()


def test_npm_cache_is_not_transferred(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    cache = root / ".npm" / "_cacache"
    cache.mkdir(parents=True)
    (cache / "entry").write_text("temporary cache", encoding="utf-8")
    (root / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    before = create_import_archive(root, tmp_path / "import.tar")
    assert list(before) == ["package.json"]

    exported = _archive([("./.npm/_cacache/entry", b"new cache", tarfile.REGTYPE),
                         ("./package.json", b'{"name":"demo"}', tarfile.REGTYPE)])
    after = extract_export(exported, tmp_path / "export")
    assert list(after) == ["package.json"]
    assert not (tmp_path / "export" / ".npm").exists()


@pytest.mark.parametrize("kind", [tarfile.LNKTYPE, tarfile.SYMTYPE])
def test_export_skips_links_inside_excluded_nested_directory(
    tmp_path: Path, kind: bytes,
) -> None:
    relative = "frontend/node_modules/@esbuild/linux-x64/bin/esbuild"
    after = extract_export(_archive([(relative, None, kind)]), tmp_path / "export")
    assert after == {}
    assert not (tmp_path / "export" / "frontend").exists()


def test_export_excluded_files_do_not_use_output_budget(tmp_path: Path) -> None:
    exported = _archive([("frontend/node_modules/package/data", b"x", tarfile.REGTYPE)])
    staging = tmp_path / "export"
    assert extract_export(exported, staging, max_total_bytes=0) == {}
    assert not (staging / "frontend").exists()


@pytest.mark.parametrize(("relative", "exclude"), [
    ("frontend/node_modules/package/cache.jar", ()),
    ("gradlehome/caches/fastutil.jar", ("gradlehome/caches/*",)),
])
def test_export_skips_oversized_cache_and_keeps_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    relative: str, exclude: tuple[str, ...],
) -> None:
    # 缩小单文件阈值，覆盖实际的大缓存情形，避免分配真实的大文件。
    monkeypatch.setattr("coder.sandbox.transfer.MAX_FILE_BYTES", 16)
    exported = _archive([(relative, b"x" * 17, tarfile.REGTYPE),
                         ("build/libs/app.jar", b"jar", tarfile.REGTYPE)])
    staging = tmp_path / "export"
    after = extract_export(exported, staging, exclude, max_total_bytes=3, max_files=1)
    assert list(after) == ["build/libs/app.jar"]
    assert (staging / "build/libs/app.jar").read_bytes() == b"jar"
    assert not (staging / relative.split("/")[0]).exists()


def test_export_member_limit_still_counts_excluded_entries(tmp_path: Path) -> None:
    exported = _archive([(f"node_modules/cache/{index}", b"x", tarfile.REGTYPE)
                         for index in range(3)])
    with pytest.raises(SandboxError, match="export file count limit exceeded"):
        extract_export(exported, tmp_path / "export", max_files=1)


@pytest.mark.parametrize("limit", ["file_size", "file_count", "total_bytes"])
def test_export_keeps_limits_for_included_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str,
) -> None:
    monkeypatch.setattr("coder.sandbox.transfer.MAX_FILE_BYTES", 16)
    data = b"x" * 17 if limit == "file_size" else b"x"
    count = 1 if limit == "file_size" else 2
    exported = _archive([(f"src/file{index}", data, tarfile.REGTYPE)
                         for index in range(count)])
    expected = ("workspace export exceeds configured workspace limit"
                if limit == "total_bytes" else "export file or count limit exceeded")
    with pytest.raises(SandboxError, match=expected):
        extract_export(exported, tmp_path / "export",
                       max_files=1 if limit == "file_count" else 3,
                       max_total_bytes=1 if limit == "total_bytes" else 100)


def test_tar_exclusions_use_shared_unanchored_path_components() -> None:
    patterns = tar_exclude_patterns()
    assert "node_modules" in patterns
    assert "./node_modules" not in patterns
    assert {".venv", ".npm", "__pycache__"}.issubset(patterns)


def test_export_failure_preserves_parser_error_after_process_is_killed() -> None:
    original = SandboxError("unsupported export entry: frontend/node_modules/tool")
    failure = _format_export_failure(original, 1, b"")
    assert failure is original


def test_export_failure_includes_exit_code_and_validation_error() -> None:
    failure = _format_export_failure(ValueError("empty file"), 1, b"")
    assert str(failure) == (
        "Docker export failed (exit 1; no stderr output); "
        "export validation failed: empty file")


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/escape", "file:stream",
                                        "CON.txt", "a\\b", "trailing. "])
def test_export_rejects_windows_and_traversal_paths(tmp_path: Path, name: str) -> None:
    with pytest.raises(SandboxError):
        extract_export(_archive([(name, b"bad", tarfile.REGTYPE)]), tmp_path / "stage")


def test_export_rejects_links_and_case_collisions(tmp_path: Path) -> None:
    link = _archive([("link", None, tarfile.SYMTYPE)])
    with pytest.raises(SandboxError, match="unsupported export entry"):
        extract_export(link, tmp_path / "link-stage")
    hard_link = _archive([("hard-link", None, tarfile.LNKTYPE)])
    with pytest.raises(SandboxError, match="unsupported export entry"):
        extract_export(hard_link, tmp_path / "hard-link-stage")
    collision = _archive([("Readme", b"one", tarfile.REGTYPE),
                          ("README", b"two", tarfile.REGTYPE)])
    with pytest.raises(SandboxError, match="case-colliding"):
        extract_export(collision, tmp_path / "collision-stage")


def test_apply_detects_host_edit_before_any_write(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("original", encoding="utf-8")
    before = create_import_archive(root, tmp_path / "import.tar")
    after = extract_export(_archive([("a.txt", b"sandbox", tarfile.REGTYPE),
                                     ("new.txt", b"new", tarfile.REGTYPE)]),
                           tmp_path / "stage")
    (root / "a.txt").write_text("user edit", encoding="utf-8")
    with pytest.raises(SandboxError, match="changed during sandbox task"):
        apply_changes(root, tmp_path / "stage", before, after)
    assert not (root / "new.txt").exists()


def test_policy_rejects_unsupported_network_and_docker_fails_closed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SandboxError, match="sandbox network must be none or allowlist"):
        load_policy({"CODER_SANDBOX_NETWORK": "bridge"})
    monkeypatch.setattr("coder.sandbox.docker.shutil.which", lambda _: None)
    with pytest.raises(SandboxError, match="Docker CLI"):
        doctor(SandboxPolicy(state_dir=workspace.parent / "state"))


def test_agent_routes_workspace_tools_to_runtime(workspace: Path) -> None:
    class FakeRuntime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
            self.calls.append((name, arguments))
            return ToolResult("", name, "from container")

        def cancel(self) -> None:
            pass

        def close(self) -> None:
            pass

    runtime = FakeRuntime()
    settings = Settings("scripted", None, None, workspace)
    agent = Agent(ScriptedLLM([]), default_tools(),
                  DefaultPermissionPolicy(yes=True), settings, runtime=runtime)
    result = agent._execute_call(ToolCall("call-1", "bash", {"command": "echo hello | cat"}))

    assert result.content == "from container"
    assert result.call_id == "call-1"
    assert runtime.calls == [("bash", {"command": "echo hello | cat"})]


def test_worker_file_tools_share_one_workspace(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "WORKSPACE", workspace)
    written = worker.execute({"name": "write_file", "arguments":
                              {"path": "from_sandbox.txt", "content": "hello"}})
    read = worker.execute({"name": "read_file", "arguments":
                           {"path": "from_sandbox.txt"}})
    assert not written.is_error
    assert "hello" in read.content
    assert worker.execute({"name": "web_search", "arguments": {}}).is_error


def test_cli_docker_selection_does_not_fall_back_to_local(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("coder.sandbox.docker.shutil.which", lambda _: None)
    monkeypatch.setattr("coder.cli.OpenAICompatibleModel",
                        lambda *args, **kwargs: pytest.fail("model started after failed doctor"))
    monkeypatch.chdir(workspace)
    assert main(["-C", str(workspace)]) == 2
