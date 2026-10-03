"""仅 Docker Sandbox 使用的受限预览服务工具。"""

from __future__ import annotations

from typing import Any

from study_coder.tools.base import ToolContext, schema
from study_coder.types import ToolResult


class StartServiceTool:
    """向模型声明预览服务的参数格式；执行在容器 Worker 中完成。"""
    name = "start_service"
    description = "Start a workspace preview service on one configured loopback port."
    is_read_only = False

    def __init__(self, ports: tuple[int, ...]) -> None:
        self.ports = ports

    def schema(self) -> dict[str, Any]:
        return schema(self.name, self.description,
                      {"command": {"type": "string"},
                       "port": {"type": "integer", "enum": list(self.ports)}},
                      ["command", "port"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 正常路径由 Agent._execute_call 转发给 DockerRuntime；直接调用要失败。
        return ToolResult("", self.name, "Error: service tools require Docker Sandbox", True)


class StopServiceTool:
    """只接收 start_service 返回的 service_id；生命周期由 Worker 管理。"""
    name = "stop_service"
    description = "Stop a preview service by the service_id returned from start_service."
    is_read_only = False

    def schema(self) -> dict[str, Any]:
        return schema(self.name, self.description,
                      {"service_id": {"type": "string"}}, ["service_id"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 与 StartServiceTool 相同，这个 fallback 防止绕过 Sandbox 运行服务工具。
        return ToolResult("", self.name, "Error: service tools require Docker Sandbox", True)
