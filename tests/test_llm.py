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
    to_provider_messages,
)
from study_coder.types import Message


def chunk(*, text: str | None = None, calls: list | None = None) -> Obj:
    return Obj(choices=[Obj(delta=Obj(content=text, tool_calls=calls or []))], usage=None)


def fragment(index: int, *, id: str | None = None, name: str | None = None,
             arguments: str | None = None) -> Obj:
    return Obj(index=index, id=id, function=Obj(name=name, arguments=arguments))


def test_stream_collects_split_calls() -> None:
    stream = [chunk(text="look", calls=[fragment(0, id="a", name="read_file", arguments='{"pa')]),
              chunk(text="ing", calls=[fragment(0, arguments='th":"x.py"}')]),
              chunk(calls=[fragment(1, id="b", name="glob", arguments='{"pattern":"*.py"}')])]
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
        collect_stream([chunk(calls=[fragment(0, id="a", name="x", arguments="{")])])
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
