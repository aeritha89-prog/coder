"""Agent、模型适配器和工具之间传递的最小数据契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolCall:
    """模型提出的一次工具调用；id 用来关联后续的工具结果。"""

    id: str
    name: str
    # 保留解析后的 JSON 对象，执行工具前不再处理未完成的参数片段。
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """对话中的一条消息；assistant 可以同时携带多个工具调用。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # 仅 role=tool 时用于指向对应 ToolCall.id。
    call_id: str | None = None


@dataclass(frozen=True)
class Usage:
    """模型报告的输入与输出 token 用量；未报告时由上层使用 None。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class LLMReply:
    """一次模型响应，同时提供展示文本和可直接写入历史的 assistant 消息。"""

    text: str
    assistant_message: Message
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果；失败也作为观察消息交还给模型。"""

    call_id: str
    name: str
    content: str
    is_error: bool = False

    @property
    def as_message(self) -> Message:
        """转成模型协议要求的 tool 消息，并保留调用 ID。"""
        return Message(role="tool", content=self.content, call_id=self.call_id)


@dataclass
class AgentRunResult:
    """一次用户任务的结果，包含停止原因、完整历史及本次工具观察。"""

    text: str
    stop_reason: Literal["completed", "round_limit", "failed", "interrupted",'incomplete']
    rounds: int
    history: list[Message]
    tool_results: list[ToolResult] = field(default_factory=list)
    usage: Usage | None = None
    # 本次任务中已开始处理的工具请求次数；拒绝和失败计入，中断后跳过的不计入。
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    # 本轮未完成的机器标签：tasks_incomplete / background_running /
    # background_unobserved / background_unknown。CLI 据此解释原因；空元组表示
    # 没有此类未完成原因，不表示工具观察中每条命令都执行成功。
    incomplete_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """递归转为普通字典，供调用方序列化或展示。"""
        return asdict(self)
