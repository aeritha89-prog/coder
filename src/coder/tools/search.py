"""遍历可见的工作区文件，提供路径 glob 与正则内容搜索。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

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


def visible_files(root: Path) -> Iterator[Path]:
    """按稳定顺序枚举非隐藏文件，跳过解析后位于根目录外的链接。"""
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.resolve().is_relative_to(root):
            yield path


def matches(path: Path, pattern: str) -> bool:
    """补足 pathlib 对 **/ 前缀匹配根目录文件时的差异。"""
    return path.match(pattern) or (pattern.startswith("**/") and path.match(pattern[3:]))


class GlobTool:
    """按路径模式查找文件，并报告总数和因上限省略的数量。"""

    name = "glob"
    description = "Find workspace files by glob pattern."
    is_read_only = True

    def schema(self) -> dict[str, Any]:
        """声明搜索模式和可选的最大结果数。"""
        return schema(self.name, self.description,
                      {"pattern": {"type": "string"}, "max_results": {"type": "integer"}},
                      ["pattern"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """限定相对工作区的模式，返回有界且排序的路径列表。"""
        def run() -> str:
            pattern = text_arg(arguments, "pattern")
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ToolFailure("glob pattern must stay in workspace")
            maximum = int_arg(arguments, "max_results", 200, 200)
            found = [p.relative_to(ctx.workspace).as_posix() for p in visible_files(ctx.workspace)
                     if matches(p.relative_to(ctx.workspace), pattern)]
            shown = found[:maximum]
            return limited("\n".join(shown) + f"\n{len(found)} matches; "
                           f"{len(found) - len(shown)} omitted",
                           ctx.settings.max_tool_output_chars)
        return observation("", self.name, run)


class GrepTool:
    """在单个文件或目录下搜索正则，输出文件、行号与匹配行。"""

    name = "grep"
    description = "Search UTF-8 workspace files with a regular expression."
    is_read_only = True

    def schema(self) -> dict[str, Any]:
        """声明正则、可选搜索起点、文件 glob 和结果上限。"""
        return schema(self.name, self.description,
                      {"pattern": {"type": "string"}, "path": {"type": "string"},
                       "glob": {"type": "string"}, "max_results": {"type": "integer"}},
                      ["pattern"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """编译正则后逐文件扫描，并统计命中、遗漏和不可读文件。"""
        def run() -> str:
            try:
                regex = re.compile(text_arg(arguments, "pattern"))
            except re.error as exc:
                raise ToolFailure(f"invalid regex: {exc}") from exc
            maximum = int_arg(arguments, "max_results", 200, 200)
            target = resolve_workspace_path(ctx.workspace,
                                            text_arg(arguments, "path") if "path" in arguments else ".")
            if not target.exists():
                raise ToolFailure("search path does not exist")
            file_pattern = arguments.get("glob", "*")
            if not isinstance(file_pattern, str) or Path(file_pattern).is_absolute() or ".." in Path(file_pattern).parts:
                raise ToolFailure("glob must be a workspace pattern")
            files = [target] if target.is_file() else visible_files(target) if target.is_dir() else []
            hits: list[str] = []
            total = 0
            skipped = 0
            for path in files:
                rel = path.relative_to(ctx.workspace)
                if not matches(rel, file_pattern):
                    continue
                try:
                    lines = read_utf8(path).splitlines()
                except ToolFailure:
                    # 二进制、超限或非 UTF-8 文件不会阻断整个搜索。
                    skipped += 1
                    continue
                for number, line in enumerate(lines, 1):
                    if regex.search(line):
                        total += 1
                        if len(hits) < maximum:
                            hits.append(f"{rel.as_posix()}:{number}: {line}")
            return limited("\n".join(hits) + f"\n{total} matches; {total - len(hits)} omitted; "
                           f"{skipped} unreadable/binary/oversize files skipped",
                           ctx.settings.max_tool_output_chars)
        return observation("", self.name, run)
