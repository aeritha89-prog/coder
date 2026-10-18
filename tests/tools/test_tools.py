from __future__ import annotations

from pathlib import Path

from study_coder.config import Settings
from study_coder.tools.base import ToolContext, ToolRegistry
from study_coder.tools.bash import SandboxBashTool
from study_coder.tools.edit import EditFileTool, WriteFileTool
from study_coder.tools.read import ReadFileTool
from study_coder.tools.search import GlobTool, GrepTool


def context(workspace: Path, **overrides: int) -> ToolContext:
    return ToolContext(workspace, workspace, Settings("scripted", None, None, workspace, **overrides))


def test_registry_rejects_duplicate() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    try:
        registry.register(ReadFileTool())
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate was accepted")
    schema = registry.schemas()[0]["function"]
    assert schema["parameters"]["type"] == "object"
    assert schema["parameters"]["required"] == ["path"]


def test_read_and_search(workspace: Path) -> None:
    ctx = context(workspace)
    found = GlobTool().execute({"pattern": "**/*.py"}, ctx).content
    assert "calculator.py" in found and "tests/test_calculator.py" in found
    assert "calculator.py:1:" in GrepTool().execute({"pattern": "def divide"}, ctx).content
    assert GrepTool().execute({"pattern": "(", "path": "calculator.py"}, ctx).is_error
    assert GrepTool().execute({"pattern": "x", "path": "absent.py"}, ctx).is_error
    read = ReadFileTool().execute({"path": "calculator.py", "start_line": 1, "end_line": 2}, ctx)
    assert "1: def divide" in read.content
    assert "3:" not in read.content


def test_path_escape_and_encoding(workspace: Path) -> None:
    ctx = context(workspace)
    assert ReadFileTool().execute({"path": "../secret.txt"}, ctx).is_error
    assert ReadFileTool().execute({"path": str(workspace / "calculator.py")}, ctx).is_error
    (workspace / "binary.dat").write_bytes(b"a\0b")
    nul_result = ReadFileTool().execute({"path": "binary.dat"}, ctx)
    assert nul_result.is_error and "contains NUL characters" in nul_result.content
    assert EditFileTool().execute({"path": "binary.dat", "old_text": "a", "new_text": "x"},
                                  ctx).is_error
    assert (workspace / "binary.dat").read_bytes() == b"a\0b"
    (workspace / "invalid.dat").write_bytes(b"\xff")
    invalid_result = ReadFileTool().execute({"path": "invalid.dat"}, ctx)
    assert invalid_result.is_error and "not UTF-8" in invalid_result.content
    (workspace / "chinese.txt").write_text("中文\n", encoding="utf-8")
    assert "中文" in ReadFileTool().execute({"path": "chinese.txt"}, ctx).content


def test_edit_unique_and_ambiguous(workspace: Path) -> None:
    ctx = context(workspace)
    path = workspace / "calculator.py"
    before = path.read_text()
    tool = EditFileTool()
    assert tool.execute({"path": "calculator.py", "old_text": "return None", "new_text": "x"}, ctx).is_error
    assert path.read_text() == before
    assert tool.execute({"path": "calculator.py", "old_text": "absent", "new_text": "x"}, ctx).is_error
    result = tool.execute({"path": "calculator.py", "old_text": "    if b == 0:\n        return None",
                           "new_text": "    if b == 0:\n        raise ZeroDivisionError()"}, ctx)
    assert not result.is_error and "+++ b/calculator.py" in result.content
    assert "raise ZeroDivisionError" in path.read_text()


def test_write_requires_overwrite(workspace: Path) -> None:
    ctx = context(workspace)
    tool = WriteFileTool()
    assert not tool.execute({"path": "note.txt", "content": "hello"}, ctx).is_error
    assert tool.execute({"path": "note.txt", "content": "bye"}, ctx).is_error
    assert (workspace / "note.txt").read_text() == "hello"
    assert not tool.execute({"path": "note.txt", "content": "bye", "overwrite": True}, ctx).is_error
    assert tool.execute({"path": "../outside.txt", "content": "x"}, ctx).is_error


def test_bash_schema_only_and_no_host_execution(workspace: Path) -> None:
    tool = SandboxBashTool()
    parameters = tool.schema()["function"]["parameters"]
    assert parameters["required"] == ["command"]
    assert parameters["properties"]["timeout_s"]["maximum"] == 86_400
    result = tool.execute({"command": "touch should-not-exist"}, context(workspace))
    assert result.is_error and "Docker runtime is not attached" in result.content
    assert not (workspace / "should-not-exist").exists()
