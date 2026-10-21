"""读取工作区内的 UTF-8 文件，并给每一行标注原始行号。"""

from __future__ import annotations

from typing import Any

from coder.tools.base import (
    ToolContext,
    ToolFailure,
    int_arg,
    limited,
    observation,
    read_utf8,
    resolve_workspace_path,
    schema,
    text_arg,
)
from coder.types import ToolResult


class ReadFileTool:
    """只读工具；支持用闭区间行号缩小返回范围。"""

    name = "read_file"
    description = "Read a UTF-8 file with line numbers."
    is_read_only = True

    def schema(self) -> dict[str, Any]:
        """声明路径必填，起止行号可选。"""
        return schema(self.name, self.description,
                      {"path": {"type": "string"}, "start_line": {"type": "integer"},
                       "end_line": {"type": "integer"}}, ["path"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """验证路径和行号，再将编号文本作为工具观察返回。"""
        def run() -> str:
            path = resolve_workspace_path(ctx.workspace, text_arg(arguments, "path"))
            lines = read_utf8(path).splitlines()
            start = int_arg(arguments, "start_line", 1, max(len(lines), 1))
            end = int_arg(arguments, "end_line", max(len(lines), 1), max(len(lines), 1))
            if start > end:
                raise ToolFailure("start_line is greater than end_line")
            # 行号从 1 开始；空文件仍返回文件名和总行数。
            rendered = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
            return limited(f"{path.relative_to(ctx.workspace)} ({len(lines)} lines)\n{rendered}",
                           ctx.settings.max_tool_output_chars)
        return observation("", self.name, run)
