"""后台工具只声明模型接口；命令及进程始终由 DockerRuntime 管理。"""

from __future__ import annotations

from typing import Any

from study_coder.tools.base import ToolContext, schema
from study_coder.types import ToolResult

BACKGROUND_TOOLS = frozenset({"start_background", "get_background", "wait_background",
                              "cancel_background"})


class BackgroundTool:
    """共用 schema 包装，避免四个工具重复 Docker fallback 逻辑。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.is_read_only = name in {"get_background", "wait_background"}
        self.description = {
            "start_background": "Start a finite command in the Docker sandbox and return immediately. "
                                "Other commands and edits may continue; order dependent operations.",
            "get_background": "Read a background job's state and bounded output tail; omit job_id "
                              "to list active jobs and unread results.",
            "wait_background": "Wait for one background job without repeated model calls, then "
                               "return its exit code and bounded output tail.",
            "cancel_background": "Cancel one background job and confirm cleanup; other jobs continue.",
        }[name]

    def schema(self) -> dict[str, Any]:
        if self.name == "start_background":
            properties = {"command": {"type": "string", "maxLength": 16384},
                          "timeout_s": {"type": "integer", "minimum": 1, "default": 60}}
            required = ["command"]
        else:
            properties = {"job_id": {"type": "string"}}
            required = [] if self.name == "get_background" else ["job_id"]
            if self.is_read_only:
                properties["tail_chars"] = {"type": "integer", "minimum": 0,
                                            "maximum": 8000, "default": 4000}
        return schema(self.name, self.description, properties, required)

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult("", self.name, "Error: background tools require Docker Sandbox", True)


def background_tools() -> list[BackgroundTool]:
    return [BackgroundTool(name) for name in
            ("start_background", "get_background", "wait_background", "cancel_background")]
