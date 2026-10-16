"""受工作区约束的文本修改工具，返回可审阅的统一格式差异。"""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import Any

from study_coder.tools.base import (
    ToolContext,
    ToolFailure,
    limited,
    observation,
    read_utf8,
    resolve_workspace_path,
    schema,
    text_arg,
)
from study_coder.types import ToolResult


def atomic_write(path: Path, content: str) -> None:
    """先在目标目录写临时文件，再原子替换，失败时清理残留。"""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(content.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def diff(path: str, before: str, after: str) -> str:
    """为编辑前后内容生成带文件名的 unified diff。"""
    return "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                        after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))


class EditFileTool:
    """仅当 old_text 在原文件恰好出现一次时执行替换。"""

    name = "edit_file"
    description = "Replace exactly one occurrence of old_text in a UTF-8 file."
    is_read_only = False

    def schema(self) -> dict[str, Any]:
        """要求路径、待替换原文和替换后的文本。"""
        return schema(self.name, self.description,
                      {key: {"type": "string"} for key in ("path", "old_text", "new_text")},
                      ["path", "old_text", "new_text"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """先验证唯一锚点，再写入并返回差异；失败时原文件保持不变。"""
        def run() -> str:
            path = resolve_workspace_path(ctx.workspace, text_arg(arguments, "path"))
            before = read_utf8(path)
            old = text_arg(arguments, "old_text")
            new = text_arg(arguments, "new_text", allow_empty=True)
            count = before.count(old)
            if count != 1:
                # 避免模型给出的模糊锚点误改多个位置。
                raise ToolFailure(f"old_text matched {count} times; provide a unique exact anchor")
            after = before.replace(old, new, 1)
            if read_utf8(path) != before:
                raise ToolFailure("文件在编辑期间发生变化，请重新读取后编辑")
            atomic_write(path, after)
            return limited(diff(path.relative_to(ctx.workspace).as_posix(), before, after),
                           ctx.settings.max_tool_output_chars)
        return observation("", self.name, run)


class WriteFileTool:
    """创建新文件；已有文件必须显式传入 overwrite=true。"""

    name = "write_file"
    description = "Create or explicitly overwrite a UTF-8 file."
    is_read_only = False

    def schema(self) -> dict[str, Any]:
        """content 可为空；overwrite 是可选布尔开关。"""
        return schema(self.name, self.description,
                      {"path": {"type": "string"}, "content": {"type": "string"},
                       "overwrite": {"type": "boolean"}}, ["path", "content"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """检查目标目录及覆盖意图，随后原子写入 UTF-8 内容。"""
        def run() -> str:
            path = resolve_workspace_path(ctx.workspace, text_arg(arguments, "path"))
            if not path.parent.is_dir():
                # 不隐式创建目录，使模型明确知道目标路径是否存在。
                raise ToolFailure("parent directory does not exist")
            content = text_arg(arguments, "content", allow_empty=True)
            overwrite = arguments.get("overwrite", False)
            if not isinstance(overwrite, bool):
                raise ToolFailure("overwrite must be boolean")
            if path.exists() and not overwrite:
                raise ToolFailure("file exists; set overwrite=true to replace it")
            before = read_utf8(path) if path.exists() else ""
            atomic_write(path, content)
            return limited(diff(path.relative_to(ctx.workspace).as_posix(), before, content),
                           ctx.settings.max_tool_output_chars)
        return observation("", self.name, run)
