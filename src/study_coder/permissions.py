"""有副作用工具的授权策略；只读工具在 Agent 中直接执行。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

Mode = Literal["interactive", "one_shot", "plan"]
Decision = Literal["allow_once", "allow_session", "deny"]


@dataclass(frozen=True)
class PermissionRequest:
    """待授权的工具及参数，供交互提示展示。"""

    tool_name: str
    arguments: dict[str, Any]
    mode: Mode


class PermissionPolicy(Protocol):
    """可替换的授权接口，便于在测试中注入固定决策。"""

    def decide(self, request: PermissionRequest) -> Decision: ...


class DefaultPermissionPolicy:
    """计划模式禁止副作用；交互模式可按次或按工具名授权。"""

    def __init__(self, *, yes: bool = False,
                 ask: Callable[[PermissionRequest], Decision] | None = None) -> None:
        """yes 预授权本次运行，ask 是交互确认回调。"""
        self.yes = yes
        self.ask = ask
        self._session_allowed: set[str] = set()

    def decide(self, request: PermissionRequest) -> Decision:
        """先应用模式限制，再查会话授权，最后询问用户或默认拒绝。"""
        if request.mode == "plan":
            return "deny"
        if request.tool_name in self._session_allowed or self.yes:
            return "allow_once"
        if request.mode == "one_shot" or self.ask is None:
            return "deny"
        decision = self.ask(request)
        if decision == "allow_session":
            # 缓存仅在当前策略对象中生效，不写入会话文件。
            self._session_allowed.add(request.tool_name)
        return decision
