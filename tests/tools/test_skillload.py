from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from coder.config import Settings
from coder.tools.base import ToolContext, ToolFailure
from coder.tools.skillload import parse_skill, skill_loader


def context(root: Path) -> ToolContext:
    return ToolContext(root, root, Settings("scripted", None, None, root))


def write_skill(root: Path, name: str, content: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("newline,bom", [("\n", False), ("\r\n", False), ("\n", True)])
def test_parse_preserves_body_and_internal_delimiter(tmp_path: Path, newline: str,
                                                    bom: bool) -> None:
    content = "---\nname: example\ndescription: Test skill\n---\n\n# Body\n---\nEnd\n"
    path = tmp_path / "SKILL.md"
    path.write_bytes((("\ufeff" if bom else "") + content.replace("\n", newline)).encode("utf-8"))
    metadata, body = parse_skill(path)
    assert metadata == "---\nname: example\ndescription: Test skill\n---\n"
    assert body == "\n# Body\n---\nEnd\n"


@pytest.mark.parametrize("content,reason", [
    ("", "must start"),
    ("# Body without frontmatter\n", "must start"),
    ("---invalid\nname: example\ndescription: Test skill\n---\nBody", "must start"),
    ("---\nname: example\nBody", "missing its closing"),
    ("---\nname: example\ndescription: Test skill\n---invalid\nBody", "missing its closing"),
    ("---\n---\nBody", "frontmatter is empty"),
    ("---\n \n---\nBody", "frontmatter is empty"),
    ("---\nname: example\ndescription: Test skill\n---", "body is empty"),
    ("---\nname: example\ndescription: Test skill\n---\n \n", "body is empty"),
])
def test_parse_rejects_invalid_boundaries(tmp_path: Path, content: str, reason: str) -> None:
    path = write_skill(tmp_path, "example", content)
    with pytest.raises(ToolFailure, match=reason):
        parse_skill(path)


def test_parse_reports_missing_and_invalid_encoding(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    with pytest.raises(ToolFailure, match="does not exist"):
        parse_skill(path)
    path.write_bytes(b"---\nname: example\ndescription: Test skill\n---\n\xff")
    with pytest.raises(ToolFailure, match="not UTF-8"):
        parse_skill(path)


@pytest.mark.parametrize("error,reason", [
    (PermissionError("denied"), "not readable"),
    (IsADirectoryError("directory"), "not a file"),
    (OSError("disk failure"), "could not be read"),
])
def test_parse_classifies_file_errors(tmp_path: Path, error: OSError, reason: str) -> None:
    with patch.object(Path, "read_text", side_effect=error):
        with pytest.raises(ToolFailure, match=reason):
            parse_skill(tmp_path / "SKILL.md")


def test_bad_skill_does_not_disable_valid_skill(tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    write_skill(tmp_path, "good", "---\nname: good\ndescription: Test skill\n---\nFull body\n")
    write_skill(tmp_path, "bad", "Body without frontmatter")
    tool = skill_loader(tmp_path)
    result = tool.execute({"skill_name": "good"}, context(tmp_path))
    assert not result.is_error and result.content == "Full body\n"
    broken = tool.execute({"skill_name": "bad"}, context(tmp_path))
    assert broken.is_error and "must start" in broken.content
    unknown = tool.execute({"skill_name": "unknown"}, context(tmp_path))
    assert unknown.is_error and "skill not found" in unknown.content
    assert tool.schema()["function"]["parameters"]["properties"]["skill_name"]["enum"] == ["good"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[技能加载]" in captured.err and "skipped bad" in captured.err


@pytest.mark.parametrize("arguments", [
    {}, {"skill_name": ""}, {"skill_name": "  \t"}, {"skill_name": None},
    {"skill_name": 123}, {"skill_name": []}, {"skill_name": True}, None, [], "good",
])
def test_invalid_arguments_become_tool_errors(tmp_path: Path, arguments: Any) -> None:
    write_skill(tmp_path, "good", "---\nname: good\ndescription: Test skill\n---\nBody")
    result = skill_loader(tmp_path).execute(arguments, context(tmp_path))
    assert result.is_error
    assert result.content.startswith("Error:")
    assert "skill_name" in result.content or "arguments must be an object" in result.content


@pytest.mark.parametrize("failure", ["missing", "file", "permission", "discovery"])
def test_directory_failure_is_recorded_without_crashing(tmp_path: Path, failure: str,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "skills"
    if failure == "missing":
        tool = skill_loader(root)
    elif failure == "file":
        root.write_text("not a directory", encoding="utf-8")
        tool = skill_loader(root)
    elif failure == "permission":
        with patch.object(Path, "stat", side_effect=PermissionError("denied")):
            tool = skill_loader(root)
    else:
        root.mkdir()
        with patch.object(Path, "glob", side_effect=OSError("discovery failed")):
            tool = skill_loader(root)
    assert tool.skills == {}
    assert tool.discovery_error is not None
    result = tool.execute({"skill_name": "good"}, context(tmp_path))
    assert result.is_error and "directory could not be loaded" in result.content
    captured = capsys.readouterr()
    assert captured.out == "" and "[技能加载]" in captured.err


def test_file_read_failure_is_isolated(tmp_path: Path) -> None:
    write_skill(tmp_path, "good", "---\nname: good\ndescription: Test skill\n---\nBody")
    bad = write_skill(tmp_path, "bad", "---\nname: bad\ndescription: Test skill\n---\nBody")
    original = Path.read_text

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == bad:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", read):
        tool = skill_loader(tmp_path)
    assert not tool.execute({"skill_name": "good"}, context(tmp_path)).is_error
    result = tool.execute({"skill_name": "bad"}, context(tmp_path))
    assert result.is_error and "not readable" in result.content


@pytest.mark.parametrize("block", [
    "name: example\ndescription: [unterminated",
    "- example\n- description",
    "name: example",
    "name: 123\ndescription: test",
    "name: example\ndescription: []",
    "name: example\ndescription: ' '",
    "name: example\nname: other\ndescription: test",
    "name: ../escape\ndescription: test",
    "name: !!python/object:builtins.str {}\ndescription: test",
])
def test_rejects_invalid_yaml_fields(tmp_path: Path, block: str) -> None:
    path = write_skill(tmp_path, "example", f"---\n{block}\n---\nBody")
    with pytest.raises(ToolFailure):
        parse_skill(path)


def test_only_skill_md_and_stable_catalog(tmp_path: Path) -> None:
    write_skill(tmp_path, "z", "---\nname: z\ndescription: Z skill\n---\nZ body")
    write_skill(tmp_path, "a", "---\nname: a\ndescription: A skill\n---\nA body")
    (tmp_path / "a" / "README.md").write_text("would overwrite the skill", encoding="utf-8")
    tool = skill_loader(tmp_path)
    assert list(tool.skills) == ["a", "z"]
    assert tool.skills["a"]["body"] == "A body"
    assert skill_loader.from_catalog(tool.catalog()).schema() == tool.schema()


def test_duplicate_names_disable_all_copies(tmp_path: Path) -> None:
    for folder in ("first", "second", "third"):
        write_skill(tmp_path, folder, "---\nname: duplicate\ndescription: test\n---\nBody")
    tool = skill_loader(tmp_path)
    assert tool.skills == {}
    result = tool.execute({"skill_name": "duplicate"}, context(tmp_path))
    assert result.is_error and "duplicate skill name" in result.content


def test_skill_limit_rejects_without_clipping(tmp_path: Path) -> None:
    from dataclasses import replace

    write_skill(tmp_path, "good", "---\nname: good\ndescription: test\n---\n1234567890")
    ctx = context(tmp_path)
    ctx = ToolContext(ctx.workspace, ctx.cwd, replace(ctx.settings, max_skill_chars=5))
    result = skill_loader(tmp_path).execute({"skill_name": "good"}, ctx)
    assert result.is_error and "exceeds max_skill_chars" in result.content
    assert "[truncated" not in result.content


def test_empty_catalog_does_not_register_tool(tmp_path: Path) -> None:
    from coder.tools import default_tools

    assert default_tools(skill_catalog={}).get("skill_loader") is None
    assert default_tools(skill_catalog=skill_loader(tmp_path).catalog()).get("skill_loader") is None
