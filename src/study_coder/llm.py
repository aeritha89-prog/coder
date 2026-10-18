"""对接 OpenAI 兼容接口，并将普通与流式响应统一为内部消息。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol
from urllib.parse import urlparse

from study_coder.config import Settings
from study_coder.types import LLMReply, Message, ToolCall, Usage


class ModelProtocolError(ValueError):
    """供应方返回的工具参数或调用结构不符合预期。"""


class ChatModel(Protocol):
    """Agent 只依赖此接口，可替换真实模型或离线测试模型。"""

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMReply: ...


class ScriptedLLM:
    """仅用于测试与离线演示的模型替身，不连接真实模型服务。

    依次返回预设响应，并记录收到的消息视图以验证 Agent 行为。
    正常 CLI 运行使用下面的 OpenAICompatibleModel。
    """

    def __init__(self, replies: Iterable[LLMReply]) -> None:
        """把回复序列转为迭代器，耗尽后报错而不伪造答复。"""
        self._replies = iter(replies)
        self.requests: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMReply:
        """记录一次请求并取出下一条预设回复。"""
        self.requests.append(list(messages))
        try:
            return next(self._replies)
        except StopIteration as exc:
            raise RuntimeError("ScriptedLLM has no more replies") from exc


def to_provider_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """将内部消息转换为 Chat Completions 的角色和工具字段。"""
    result: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.role == "assistant" and message.tool_calls:
            # assistant 的每个调用必须带 id、函数名和 JSON 字符串参数。
            item["tool_calls"] = [{"id": call.id, "type": "function",
                                   "function": {"name": call.name,
                                                "arguments": json.dumps(call.arguments)}}
                                  for call in message.tool_calls]
        if message.role == "tool":
            # tool_call_id 与前一个 assistant 工具调用的 id 对应。
            item["tool_call_id"] = message.call_id
        result.append(item)
    return result


def _reply(text: str, calls: list[ToolCall], usage: Usage | None = None,
           finish_reason: str | None = None) -> LLMReply:
    """让展示文本、历史消息和工具列表来自同一份解析结果。"""
    message = Message("assistant", text, tuple(calls))
    return LLMReply(text, message, tuple(calls), usage, finish_reason)


def _finish_reason(choice: Any) -> str | None:
    """兼容未提供该字段的服务，但拒绝非字符串结束原因。"""
    reason = getattr(choice, "finish_reason", None)
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise ModelProtocolError("model returned invalid finish_reason")
    return reason


def normalize_completion(completion: Any) -> LLMReply:
    """解析非流式响应；工具参数必须是完整的 JSON 对象。"""
    choice = completion.choices[0]
    reason = _finish_reason(choice)
    message = choice.message
    raw_usage = getattr(completion, "usage", None)
    usage = (Usage(raw_usage.prompt_tokens, raw_usage.completion_tokens)
             if raw_usage is not None else None)
    # 即使被截断的参数恰好能解析为 JSON，也不能执行：后续片段可能尚未生成。
    if reason not in (None, "stop", "tool_calls"):
        return _reply(message.content or "", [], usage, reason)
    calls: list[ToolCall] = []
    for raw in message.tool_calls or []:
        try:
            arguments = json.loads(raw.function.arguments)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError("model returned invalid tool arguments JSON") from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError("tool arguments must be a JSON object")
        calls.append(ToolCall(raw.id, raw.function.name, arguments))
    if reason == "tool_calls" and not calls:
        reason = "stream_incomplete"
    return _reply(message.content or "", calls, usage, reason)


def collect_stream(chunks: Iterable[Any], on_text: Callable[[str], None] | None = None) -> LLMReply:
    """按索引拼接流式工具调用，收流后才解析 JSON 并允许执行。

    文本片段可以即时送给显示回调；函数名、调用 ID 和参数可能跨多个
    chunk 到达，不能把中间片段当作一条完整的工具请求。
    """
    parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    usage: Usage | None = None
    finish_reason: str | None = None
    for chunk in chunks:
        raw_usage = getattr(chunk, "usage", None)
        if raw_usage is not None:
            usage = Usage(raw_usage.prompt_tokens, raw_usage.completion_tokens)
        if not chunk.choices:
            # 部分供应方在无 choice 的尾块中单独发送 token 用量。
            continue
        choice = chunk.choices[0]
        reason = _finish_reason(choice)
        if reason is not None:
            if finish_reason is not None and reason != finish_reason:
                raise ModelProtocolError("stream returned conflicting finish reasons")
            finish_reason = reason
        delta = choice.delta
        if delta.content:
            parts.append(delta.content)
            if on_text:
                on_text(delta.content)
        for raw in delta.tool_calls or []:
            # 一个流中可交错生成多个工具调用，index 是各调用的稳定位置。
            current = calls.setdefault(raw.index, {"id": "", "name": "", "arguments": ""})
            if raw.id:
                current["id"] += raw.id
            function = raw.function
            if function is not None:
                if function.name:
                    current["name"] += function.name
                if function.arguments:
                    current["arguments"] += function.arguments
    # TCP/迭代器结束不等于模型正常结束。保留已显示文本，但丢弃尚不能确认
    # 完整性的工具调用，历史中也不会留下没有对应结果的 assistant 调用组。
    if finish_reason not in ("stop", "tool_calls"):
        return _reply("".join(parts), [], usage, finish_reason or "stream_incomplete")
    parsed: list[ToolCall] = []
    # 只有收完所有块后才检查调用完整性，避免执行半截 JSON。
    for index in sorted(calls):
        item = calls[index]
        try:
            arguments = json.loads(item["arguments"])
        except json.JSONDecodeError as exc:
            raise ModelProtocolError(f"incomplete tool arguments at index {index}") from exc
        if not item["id"] or not item["name"] or not isinstance(arguments, dict):
            raise ModelProtocolError(f"invalid tool call at index {index}")
        parsed.append(ToolCall(item["id"], item["name"], arguments))
    if finish_reason == "tool_calls" and not parsed:
        finish_reason = "stream_incomplete"
    return _reply("".join(parts), parsed, usage, finish_reason)


def should_retry(error: Exception) -> bool:
    """只重试连接、超时、限流及服务器错误，保留其他异常供上层处理。"""
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
    return isinstance(error, (APIConnectionError, APITimeoutError, InternalServerError,
                              RateLimitError))


def is_local_ollama_url(base_url: str | None) -> bool:
    """仅识别回环地址上的 Ollama v1 接口，可使用本地占位 key。"""
    if not base_url:
        return False
    parsed = urlparse(base_url)
    return (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.port == 11434 and parsed.path.rstrip("/") == "/v1")


class OpenAICompatibleModel:
    """通过 OpenAI SDK 调用兼容接口；默认可连接本地 Ollama。"""

    def __init__(self, settings: Settings, *, stream: bool = False,
                 on_text: Callable[[str], None] | None = None, client: Any = None) -> None:
        """可注入 client 以离线测试；远端接口必须配置真实 API key。"""
        if client is None:
            api_key = settings.api_key or ("ollama" if is_local_ollama_url(settings.base_url)
                                            else None)
            if not api_key:
                raise ValueError("API key required for nonlocal endpoint: set STUDY_CODER_API_KEY")
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=settings.base_url,
                            timeout=settings.request_timeout_s, max_retries=0)
        self.client = client
        self.settings = settings
        self.stream = stream
        self.on_text = on_text
        self._require_tool = False

    @contextmanager
    def response_options(self, *, require_tool: bool = False, defer_text: bool = False
                         ) -> Iterator[Callable[[str], None] | None]:
        """临时设置当前请求的工具要求，并可暂存文本直到 Agent 核验完成。

        未验证的记忆草稿不能先打印再撤回。调用方只在通过核验后使用返回的
        显示回调；异常和中断也会恢复正常对话的逐片输出设置。
        """
        previous_required, previous_text = self._require_tool, self.on_text
        self._require_tool = require_tool
        if defer_text:
            self.on_text = None
        try:
            yield previous_text if defer_text and self.stream else None
        finally:
            self._require_tool, self.on_text = previous_required, previous_text

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMReply:
        """最多尝试三次可重试错误，再把统一的 LLMReply 交给 Agent。"""
        kwargs: dict[str, Any] = {"model": self.settings.model,
                                  "messages": to_provider_messages(messages),
                                  "tools": tools, "stream": self.stream}
        if self._require_tool:
            kwargs["tool_choice"] = "required"
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(**kwargs)
                return (collect_stream(response, self.on_text) if self.stream
                        else normalize_completion(response))
            except Exception as exc:
                if attempt == 2 or not should_retry(exc):
                    raise
                # 简单指数退避，避免连接抖动时立即重复请求。
                time.sleep(0.25 * (2 ** attempt))
        raise AssertionError("unreachable")
