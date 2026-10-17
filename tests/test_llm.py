from __future__ import annotations

from types import SimpleNamespace as Obj

import pytest

from study_coder.config import Settings
from study_coder.llm import (
    ModelProtocolError,
    OpenAICompatibleModel,
    ScriptedLLM,
    collect_stream,
    is_local_ollama_url,
    normalize_completion,
    to_provider_messages,
)
from study_coder.types import Message


def chunk(*, text: str | None = None, calls: list | None = None,
          finish_reason: str | None = None) -> Obj:
    return Obj(choices=[Obj(delta=Obj(content=text, tool_calls=calls or []),
                            finish_reason=finish_reason)], usage=None)


def fragment(index: int, *, id: str | None = None, name: str | None = None,
             arguments: str | None = None) -> Obj:
    return Obj(index=index, id=id, function=Obj(name=name, arguments=arguments))


def test_stream_collects_split_calls() -> None:
    stream = [chunk(text="look", calls=[fragment(0, id="a", name="read_file", arguments='{"pa')]),
              chunk(text="ing", calls=[fragment(0, arguments='th":"x.py"}')]),
              chunk(calls=[fragment(1, id="b", name="glob", arguments='{"pattern":"*.py"}')]),
              chunk(finish_reason="tool_calls")]
    emitted: list[str] = []
    reply = collect_stream(stream, emitted.append)
    assert reply.text == "looking" and emitted == ["look", "ing"]
    assert [call.arguments for call in reply.tool_calls] == [{"path": "x.py"}, {"pattern": "*.py"}]
    provider = to_provider_messages([reply.assistant_message,
                                     Message("tool", "content", call_id="a")])
    assert provider[0]["tool_calls"][0]["id"] == "a"
    assert provider[1]["tool_call_id"] == "a"


def test_invalid_json_and_script_exhaustion() -> None:
    with pytest.raises(ModelProtocolError):
        collect_stream([chunk(calls=[fragment(0, id="a", name="x", arguments="{")]),
                        chunk(finish_reason="tool_calls")])
    with pytest.raises(RuntimeError):
        ScriptedLLM([]).complete([], [])


def test_adapter_normalizes_completion_without_network(workspace) -> None:
    captured: list[dict] = []

    def create(**kwargs):
        captured.append(kwargs)
        message = Obj(content="", tool_calls=[Obj(id="call-1",
                                                 function=Obj(name="read_file",
                                                              arguments='{"path":"a.py"}'))])
        return Obj(choices=[Obj(message=message)],
                   usage=Obj(prompt_tokens=9, completion_tokens=4))

    client = Obj(chat=Obj(completions=Obj(create=create)))
    model = OpenAICompatibleModel(Settings("fake", None, None, workspace), client=client)
    result = model.complete([Message("user", "read a.py")], [])
    assert captured[0]["messages"] == [{"role": "user", "content": "read a.py"}]
    assert result.tool_calls[0].arguments == {"path": "a.py"}
    assert result.usage.prompt_tokens == 9


def test_local_ollama_uses_placeholder_key_only_for_loopback(workspace, monkeypatch) -> None:
    captured: list[dict] = []

    def fake_openai(**kwargs):
        captured.append(kwargs)
        return Obj()

    monkeypatch.setattr("openai.OpenAI", fake_openai)
    assert is_local_ollama_url("http://localhost:11434/v1/")
    assert not is_local_ollama_url("https://remote.example/v1")
    OpenAICompatibleModel(Settings("qwen3.5:4b", None,
                                   "http://127.0.0.1:11434/v1", workspace))
    assert captured[0]["api_key"] == "ollama"
    with pytest.raises(ValueError, match="API key required"):
        OpenAICompatibleModel(Settings("remote", None, "https://remote.example/v1", workspace))


@pytest.mark.parametrize("reason", ["length", "content_filter", "unknown_reason"])
@pytest.mark.parametrize("arguments", ['{"path":"should-not-run"}', '{"path":'])
def test_nonfinal_completion_keeps_text_and_usage_but_discards_calls(reason, arguments):
    message = Obj(content="partial text", tool_calls=[Obj(id="a", function=Obj(
        name="write_file", arguments=arguments))])
    reply = normalize_completion(Obj(choices=[Obj(message=message, finish_reason=reason)],
                                     usage=Obj(prompt_tokens=10, completion_tokens=20)))
    assert reply.interrupted and reply.finish_reason == reason
    assert reply.text == "partial text" and reply.usage.completion_tokens == 20
    assert not reply.tool_calls and not reply.assistant_message.tool_calls


@pytest.mark.parametrize("reason", [None, "length", "content_filter"])
def test_interrupted_stream_never_executes_partial_calls(reason):
    stream = [chunk(text="partial", calls=[fragment(0, id="a", name="write_file", arguments="{")])]
    if reason:
        stream.append(chunk(finish_reason=reason))
    reply = collect_stream(stream)
    assert reply.text == "partial" and reply.interrupted
    assert reply.finish_reason == (reason or "stream_incomplete")
    assert not reply.tool_calls and not reply.assistant_message.tool_calls


def test_stream_terminal_marker_survives_usage_only_tail():
    reply = collect_stream([chunk(text="done"), chunk(finish_reason="stop"),
                            Obj(choices=[], usage=Obj(prompt_tokens=5, completion_tokens=3))])
    assert not reply.interrupted and reply.finish_reason == "stop"
    assert reply.usage.prompt_tokens == 5


def test_truncated_summary_is_not_accepted():
    from study_coder.context import ContextError, ModelSummarizer
    from study_coder.types import LLMReply

    model = ScriptedLLM([LLMReply("partial", Message("assistant", "partial"), finish_reason="length")])
    with pytest.raises(ContextError, match="no usable summary"):
        ModelSummarizer(model).summarize([Message("user", "old task")])
