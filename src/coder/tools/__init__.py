"""集中注册工作区工具与独立的 Web 搜索工具。"""

from coder.tools.background import background_tools
from coder.tools.base import Tool, ToolRegistry
from coder.tools.bash import SandboxBashTool
from coder.tools.edit import EditFileTool, WriteFileTool
from coder.tools.memory import memory_tools
from coder.tools.read import ReadFileTool
from coder.tools.sandbox_service import StartServiceTool, StopServiceTool
from coder.tools.search import GlobTool, GrepTool
from coder.tools.skillload import skill_loader
from coder.tools.task import (
    claim_task,
    complete_task,
    create_task,
    delete_task,
    get_task,
    reset_task_progress,
    task_list,
    update_task_dependencies,
)
from coder.tools.websearch import web_search


def default_tools(*, allow_web_search: bool = False,
                  service_ports: tuple[int, ...] = (),
                  background_enabled: bool = True,
                  memory_enabled: bool = False,
                  skill_catalog: dict[str, dict[str, str]] | None = None) -> ToolRegistry:
    """创建独立注册表；顺序决定传给模型的工具描述顺序。"""
    registry = ToolRegistry()
    tools: list[Tool] = [ReadFileTool(), GlobTool(), GrepTool(), EditFileTool(), WriteFileTool(),
                         SandboxBashTool(), claim_task(), complete_task(), task_list(), get_task(),
                         create_task(), update_task_dependencies(), delete_task(),reset_task_progress()]
    loader = skill_loader() if skill_catalog is None else skill_loader.from_catalog(skill_catalog)
    if loader.skills:
        tools.append(loader)
    else:
        loader._log("no valid skills; skill_loader is not registered")
    if allow_web_search:
        tools.append(web_search())
    if service_ports:
        tools.extend((StartServiceTool(service_ports), StopServiceTool()))
    if background_enabled:
        # 此开关与 DockerRuntime 路由检查使用同一宿主策略；关闭时模型不看到工具。
        tools.extend(background_tools())
    if memory_enabled:
        tools.extend(memory_tools())
    for tool in tools:
        registry.register(tool)
    return registry


