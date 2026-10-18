"""故意让模型谎报保存成功：宿主必须拒绝，并只显示实际操作结果。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as Obj

import pytest

from study_coder.agent import Agent
from study_coder.config import Settings
from study_coder.llm import OpenAICompatibleModel, ScriptedLLM
from study_coder.memory import MemoryManager, MemoryStore
from study_coder.memory_action import MemoryAction
from study_coder.permissions import DefaultPermissionPolicy
from study_coder.tools import default_tools
from study_coder.types import LLMReply, Message, ToolCall


class Runtime:
    def get_task_state(self):
        return ""
    def get_incomplete_tasks(self):
        return {}
    def execute_tool(self, *args):
        raise AssertionError("memory must use the host route")


def final(text: str = "已保存，ID 是 mem_294a8b43b1e10561，always，版本 99。") -> LLMReply:
    return LLMReply(text, Message("assistant", text))


def tool(name: str, arguments: dict, identifier: str = "call") -> LLMReply:
    call = ToolCall(identifier, name, arguments)
    return LLMReply("", Message("assistant", "", (call,)), (call,))


def setup(root: Path, replies, *, yes: bool = True, max_rounds: int = 20):
    store = MemoryStore(root / "state", root)
    model = ScriptedLLM(replies)
    manager = MemoryManager(store, auto_episode=False)
    agent = Agent(model, default_tools(skill_catalog={}, memory_enabled=True), DefaultPermissionPolicy(yes=yes),
                  Settings("test", None, None, root, max_rounds=max_rounds), Runtime(), memory=manager)
    return store, model, agent


@pytest.mark.parametrize("user_text,expected,policy", [
    ("记住我喜欢吃锅包肉", "memory_remember", None),
    ("请记下我喜欢素食", "memory_remember", None),
    ("remember my food preference", "memory_remember", None),
    ("不用always", "memory_remember", "on_demand"),
    ("这条不要用 always 了", "memory_remember", "on_demand"),
    ("改成on_demand", "memory_remember", "on_demand"),
    ("改成always", "memory_remember", "always"),
    ("把这条记忆改为常驻", "memory_remember", "always"),
    ("忘记我的食物偏好", "memory_forget", None),
    ("please forget my name", "memory_forget", None),
    ("讲解一下记忆机制", None, None),
    ("请实现记忆模块", None, None),
    ("修改记忆机制", None, None),
    ("把记忆存储改成JSON", None, None),
    ("把载入策略配置的代码改掉", None, None),
    ("忘记之前的错误，继续写代码", None, None),
    ("forget about the last error and build the project", None, None),
    ("remember to run tests", None, None),
    ("不要记住这句话", None, None),
    ("把 always 变量改为 bool", None, None),
    ("保存 README.md", None, None),
])
def test_direct_memory_command_recognition(user_text, expected, policy):
    action = MemoryAction.from_request(user_text)
    assert (action.tool if action else None) == expected
    if action:
        assert action.policy == policy


@pytest.mark.parametrize("user_text", ["记住我喜欢吃锅包肉", "不用always"])
def test_zero_tool_fabricated_success_is_retried_then_rejected(tmp_path: Path, user_text: str):
    store, model, agent = setup(tmp_path, [final(), final(), final()])
    existing = store.remember("semantic", "name", "Known name", load_policy="always")
    outcome = agent.run(user_text)
    assert outcome.stop_reason == "incomplete" and outcome.tool_call_counts == {}
    assert outcome.incomplete_reasons == ("memory_operation_unconfirmed",)
    assert len(model.requests) == 3 and "未能确认" in outcome.text
    assert "mem_294a8b43b1e10561" not in repr(agent.history)
    assert "always，版本 99" not in outcome.text
    assert store.search() == [existing]
    assert any("Memory operation unconfirmed" in item.content for item in model.requests[1])
    assert not any("Memory operation unconfirmed" in item.content for item in agent.history)


def test_fabricated_success_can_recover_with_real_write_and_host_metadata(tmp_path: Path):
    request = "记住我喜欢吃锅包肉"
    store, model, agent = setup(tmp_path, [final(), tool("memory_remember", {
        "kind": "semantic", "key": "food", "content": "喜欢吃锅包肉", "evidence": request}), final()])
    outcome = agent.run(request)
    entry = store.search()[0]
    assert outcome.stop_reason == "completed" and outcome.tool_call_counts == {"memory_remember": 1}
    assert entry.content in outcome.text and entry.id in outcome.text
    assert "on_demand" in outcome.text and "版本：1" in outcome.text and "状态：active" in outcome.text
    assert "mem_294a8b43b1e10561" not in outcome.text and "版本 99" not in outcome.text
    assert agent.history[-1].content == outcome.text
    assert all("mem_294a8b43b1e10561" not in repr(messages) for messages in model.requests[1:])


def test_policy_update_requires_real_versioned_write(tmp_path: Path):
    store, model, agent = setup(tmp_path, [])
    entry = store.remember("semantic", "food", "喜欢吃锅包肉", load_policy="always", priority=90)
    replies = [final(), tool("memory_read", {"id": entry.id}, "read"), tool("memory_remember", {
        "kind": entry.kind, "key": entry.key, "content": entry.content, "load_policy": "on_demand",
        "expected_revision": entry.revision, "evidence": "不用always"}, "update"), final()]
    model._replies = iter(replies)
    outcome = agent.run("不用always")
    current = store.get(entry.id)
    assert outcome.stop_reason == "completed" and current.load_policy == "on_demand" and current.revision == 2
    assert outcome.tool_call_counts == {"memory_read": 1, "memory_remember": 1}
    assert "载入策略：on_demand" in outcome.text and "版本：2" in outcome.text
    assert entry.id in outcome.text and "mem_294a8b43b1e10561" not in outcome.text


def test_reading_existing_entry_does_not_count_as_current_write(tmp_path: Path):
    store, model, agent = setup(tmp_path, [])
    entry = store.remember("semantic", "food", "Food preference")
    model._replies = iter([tool("memory_read", {"id": entry.id}), final(), final(), final()])
    outcome = agent.run("记住我喜欢吃锅包肉")
    assert outcome.stop_reason == "incomplete" and outcome.tool_call_counts == {"memory_read": 1}
    assert store.get(entry.id) == entry and "mem_294a8b43b1e10561" not in outcome.text


def test_permission_denial_is_not_retried_or_reported_as_saved(tmp_path: Path):
    store, model, agent = setup(tmp_path, [tool("memory_remember", {
        "kind": "semantic", "key": "food", "content": "Food"}), final()], yes=False)
    outcome = agent.run("记住我的食物偏好")
    assert outcome.stop_reason == "incomplete" and outcome.tool_call_counts == {"memory_remember": 1}
    assert len(model.requests) == 1 and not store.search()


def test_query_loop_is_bounded_even_with_large_coding_round_limit(tmp_path: Path):
    _, model, agent = setup(tmp_path, [tool("memory_search", {"query": "food"}, str(i)) for i in range(10)])
    outcome = agent.run("记住我喜欢吃锅包肉")
    assert outcome.stop_reason == "incomplete" and len(model.requests) == 6
    assert outcome.tool_call_counts == {"memory_search": 6}
    agent.context.validate_tool_message_groups(agent.history)


@pytest.mark.parametrize("stream", [False, True])
def test_adapter_forces_tools_and_withholds_fabricated_stream_until_verified(tmp_path: Path, stream: bool):
    store, _, agent = setup(tmp_path, [])
    request = "记住我喜欢吃锅包肉"
    arguments = {"kind": "semantic", "key": "food", "content": "喜欢吃锅包肉", "evidence": request}
    requests, emitted = [], []
    def create(**kwargs):
        requests.append(kwargs)
        number = len(requests)
        text = "普通对话" if number == 4 else "保存好了，假 ID：mem_294a8b43b1e10561。"
        calls = [Obj(id="save", function=Obj(name="memory_remember", arguments=json.dumps(arguments)))] if number == 2 else []
        if not stream:
            return Obj(choices=[Obj(message=Obj(content="" if calls else text, tool_calls=calls),
                                    finish_reason="tool_calls" if calls else "stop")], usage=None)
        delta_calls = [Obj(index=0, id=call.id, function=call.function) for call in calls]
        return [Obj(choices=[Obj(delta=Obj(content="" if calls else text, tool_calls=delta_calls), finish_reason=None)], usage=None),
                Obj(choices=[Obj(delta=Obj(content=None, tool_calls=[]),
                                 finish_reason="tool_calls" if calls else "stop")], usage=None)]
    agent.model = OpenAICompatibleModel(agent.settings, stream=stream, on_text=emitted.append,
                                       client=Obj(chat=Obj(completions=Obj(create=create))))
    outcome = agent.run(request)
    assert outcome.stop_reason == "completed" and store.search()[0].id in outcome.text
    assert requests[0]["tool_choice"] == requests[1]["tool_choice"] == "required"
    assert {item["function"]["name"] for item in requests[0]["tools"]} == {
        "memory_read", "memory_search", "memory_remember"}
    assert "tool_choice" not in requests[2]
    assert "mem_294a8b43b1e10561" not in "".join(emitted)
    assert emitted == ([outcome.text] if stream else [])
    # 请求级设置恢复后，普通对话仍可以逐片输出。
    agent.model.complete([Message("user", "hello")], [])
    assert "tool_choice" not in requests[3]
    assert (emitted[-1] == "普通对话") if stream else not emitted


def test_forget_requires_successful_delete_receipt(tmp_path: Path):
    store, model, agent = setup(tmp_path, [])
    entry = store.remember("semantic", "food", "Food preference")
    model._replies = iter([tool("memory_forget", {"id": entry.id}), final("已删掉所有记忆。")])
    outcome = agent.run("忘记我的食物偏好")
    assert outcome.stop_reason == "completed" and store.get(entry.id) is None
    assert outcome.text == f"已删除记忆，ID：{entry.id}。"


def test_last_round_actual_write_can_complete_without_another_model_reply(tmp_path: Path):
    request = "记住我喜欢吃锅包肉"
    store, model, agent = setup(tmp_path, [tool("memory_remember", {
        "kind": "semantic", "key": "food", "content": "喜欢吃锅包肉", "evidence": request})], max_rounds=1)
    outcome = agent.run(request)
    assert outcome.stop_reason == "completed" and len(model.requests) == 1
    assert store.search()[0].id in outcome.text and outcome.tool_call_counts == {"memory_remember": 1}


def test_matching_receipt_with_wrong_actual_policy_cannot_confirm_requested_update(tmp_path: Path):
    store, model, agent = setup(tmp_path, [tool("memory_remember", {
        "kind": "semantic", "key": "food", "content": "Food"}), final(), final(), final()])
    # 模型选择了默认 on_demand；请求要求 always。不能让“有写入回执”掩盖策略不符。
    outcome = agent.run("改成always")
    assert store.search()[0].load_policy == "on_demand"
    assert outcome.stop_reason == "incomplete" and "未能确认" in outcome.text
    assert outcome.tool_call_counts == {"memory_remember": 1}


def test_candidate_receipt_reports_candidate_without_claiming_activation(tmp_path: Path):
    store, _, agent = setup(tmp_path, [tool("memory_remember", {
        "kind": "semantic", "key": "food", "content": "Food"}), final()])
    outcome = agent.run("记住我的食物偏好")
    assert outcome.stop_reason == "completed" and store.search()[0].status == "candidate"
    assert "仍是候选" in outcome.text and "尚未自动生效" in outcome.text


def test_interrupted_memory_draft_is_not_shown_as_success(tmp_path: Path):
    interrupted = LLMReply("已经保存，假 ID mem_294a8b43b1e10561", Message("assistant", "draft"), finish_reason="length")
    _, _, agent = setup(tmp_path, [interrupted])
    outcome = agent.run("记住我的食物偏好")
    assert outcome.incomplete_reasons == ("model_output_truncated",)
    assert "mem_294a8b43b1e10561" not in outcome.text and "未能确认" in outcome.text


def test_required_memory_tool_request_cannot_execute_unrelated_workspace_command(tmp_path: Path):
    _, _, agent = setup(tmp_path, [tool("bash", {"command": "touch unrelated"}), final(), final(), final()])
    outcome = agent.run("记住我的食物偏好")
    assert outcome.stop_reason == "incomplete" and outcome.tool_results[0].is_error
    assert "not available" in outcome.tool_results[0].content


def test_correction_does_not_extend_six_round_operation_budget(tmp_path: Path):
    _, model, agent = setup(tmp_path, [*[tool("memory_search", {"query": "food"}, str(i)) for i in range(5)],
                                      final(), final(), final()])
    outcome = agent.run("记住我喜欢吃锅包肉")
    assert outcome.stop_reason == "incomplete" and len(model.requests) == 6
    assert outcome.tool_call_counts == {"memory_search": 5}
