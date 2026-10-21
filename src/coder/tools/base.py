"""所有工具共用的协议、参数校验、工作区路径检查和结果包装。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from coder.config import Settings
from coder.types import ToolResult


class ToolFailure(ValueError):
    """可预期的参数或环境错误，应作为工具观察返回给模型。"""


@dataclass(frozen=True)
class ToolContext:
    """工具执行时固定的工作区、当前目录和设置。"""

    workspace: Path
    cwd: Path
    settings: Settings


class Tool(Protocol):
    """工具协议：向模型暴露 schema，并返回文本观察及错误标志。"""

    name: str
    description: str
    is_read_only: bool

    def schema(self) -> dict[str, Any]: ...

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class ToolRegistry:
    """按注册顺序向模型提供工具，拒绝重名以免执行目标不确定。"""

    def __init__(self) -> None:
        """建立名称到工具实例的映射。"""
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具，名称必须唯一。"""
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """找不到名称时返回 None，由 Agent 转为失败观察。"""
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """取得模型可见的函数描述，顺序与注册顺序一致。"""
        return [tool.schema() for tool in self._tools.values()]


def schema(name: str, description: str, properties: dict[str, Any],
           required: list[str]) -> dict[str, Any]:
    """构造 OpenAI 兼容的函数 schema，并禁止未声明参数。"""
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


def text_arg(arguments: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    """读取字符串参数；仅显式允许时接受空字符串。"""
    value = arguments.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ToolFailure(f"{key} must be a nonempty string")
    return value


def int_arg(arguments: dict[str, Any], key: str, default: int, maximum: int) -> int:
    """读取 1..maximum 的整数；bool 在 Python 中属 int，需单独拒绝。"""
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ToolFailure(f"{key} must be an integer from 1 to {maximum}")
    return value


def resolve_workspace_path(workspace: Path, user_path: str) -> Path:
    """拒绝绝对路径及解析后逃出工作区的相对路径，包括符号链接。"""
    path = Path(user_path)
    if path.is_absolute():
        raise ToolFailure("absolute paths are not allowed")
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise ToolFailure("path escapes workspace")
    return target


def limited(content: str, maximum: int) -> str:
    """控制返回给模型的字符数，并明确标记被截断的长度。"""
    if len(content) <= maximum:
        return content
    return content[:maximum] + f"\n[truncated {len(content) - maximum} characters]"


def read_utf8(path: Path, maximum_bytes: int = 2_000_000) -> str:
    """只读取存在、大小受限且不含 NUL 字符的 UTF-8 文本。"""
    if not path.is_file():
        raise ToolFailure("file does not exist")
    if path.stat().st_size > maximum_bytes:
        raise ToolFailure("file exceeds 2 MB; narrow the target")
    data = path.read_bytes()
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolFailure("file is not UTF-8") from exc
    # NUL 也可能出现在合法 UTF-8 文本里；拒绝它是文本工具的输入约束，
    # 不能据此断言文件一定是二进制文件。
    if "\0" in content:
        raise ToolFailure("file contains NUL characters; text tools do not support them")
    return content


def observation(call_id: str, name: str, work: Any) -> ToolResult:
    """把预期失败转为 is_error 观察；其他编程错误由 Agent 兜底。"""
    try:
        return ToolResult(call_id, name, work())
    except (ToolFailure, OSError, UnicodeError, ValueError) as exc:
        return ToolResult(call_id, name, f"Error: {exc}", True)
