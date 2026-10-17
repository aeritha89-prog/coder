"""宿主与任务执行后端之间的最小契约。"""

from __future__ import annotations

from typing import Any, Protocol

from study_coder.tools.background import BACKGROUND_TOOLS
from study_coder.types import ToolResult

# Agent 仅把这里的工具交给 DockerRuntime；普通工具转 Worker，后台工具转管理器
# 的独立控制入口。web_search 即使启用仍由宿主实现，不在此沙箱路由白名单中。
SANDBOX_TOOLS = frozenset({"read_file", "glob", "grep", "edit_file", "write_file", "bash", "skill_loader",
                          "start_service", "stop_service","claim_task","complete_task",
                           "task_list","get_task","create_task","update_task_dependencies","delete_task","reset_task_progress"}) | BACKGROUND_TOOLS


class SandboxError(RuntimeError):
    """执行环境、策略或导入导出边界失败。"""


class WorkspaceRuntime(Protocol):
    """Agent 可见的运行时接口。

    Agent 只提交工具名与参数；容器名、镜像、网络、资源上限等由宿主策略决定。
    具体 Docker 生命周期由 DockerRuntime 实现，不暴露给模型调用。
    """

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...

    def get_incomplete_tasks(self) -> dict[str, str]:
        ...

    def get_task_state(self) -> str:
        """返回有长度上限的任务摘要，供每轮模型请求保留。"""
        ...

    def get_export_issues(self) -> list[str]:
        """检查当前文件能否交付；不会停止容器或修改项目。"""
        ...

    def get_background_state(self) -> str:
        """返回下一轮模型所需的短状态元数据，不包含原始命令或日志。"""
        ...

    def get_background_pending(self) -> tuple[str, ...]:
        """返回后台任务导致本轮无法宣布完成的原因标签。"""
        ...

    def acknowledge_background(self, job_ids: list[str]) -> None:
        """完整历史保存后，确认这些 job_ids 的终态已向 Agent 交付。"""
        ...

    def background_job_ids(self) -> set[str]:
        """返回已登记的执行编号集合，包括活动项和终态项。"""
        ...

    def interrupt_background(self, job_ids: set[str]) -> None:
        """中断指定编号的后台命令，不把任意一个任务编号当作整沙箱停止请求。"""
        ...
