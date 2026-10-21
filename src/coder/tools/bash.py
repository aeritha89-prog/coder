"""向模型声明 Docker 容器中的 bash 工具；执行由 DockerRuntime 转发。"""

from __future__ import annotations

from typing import Any

from coder.tools.base import ToolContext, schema
from coder.types import ToolResult


class SandboxBashTool:
    """只提供工具 schema，绝不在宿主执行模型给出的命令。"""

    name = "bash"
    description = ("Run Linux development commands inside the isolated task container. "
                   "Pipes, redirection and command chaining are supported. "
                   "Optional timeout_s overrides the configured default within the sandbox limit.")
    is_read_only = False

    def schema(self) -> dict[str, Any]:
        return schema(self.name, self.description,
                      {"command": {"type": "string"},
                       "timeout_s": {"type": "integer", "minimum": 1, "maximum": 86_400,
                                     "description": "Maximum runtime in seconds for this call. "
                                                    "Omit to use the configured default (60s)."}},
                      ["command"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """直接调用不会绕过容器；正常调用由 Agent 发给 DockerRuntime。"""
        return ToolResult("", self.name, "Error: Docker runtime is not attached", True)
