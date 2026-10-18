"""Agent 主循环：将模型响应、授权、工具观察和持久历史串起来。"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from contextlib import nullcontext
from dataclasses import replace
from typing import Callable, Literal

from study_coder.config import Settings
from study_coder.context import ContextManager, ModelSummarizer
from study_coder.llm import ChatModel
from study_coder.memory import MEMORY_TOOLS, MemoryContext, MemoryManager
from study_coder.memory_action import MemoryAction
from study_coder.permissions import Mode, PermissionPolicy, PermissionRequest
from study_coder.prompt import SYSTEM_PROMPT
from study_coder.sandbox.background_runner import TERMINAL
from study_coder.sandbox.base import SANDBOX_TOOLS, WorkspaceRuntime
from study_coder.tools.background import BACKGROUND_TOOLS
from study_coder.tools.base import ToolContext, ToolFailure, ToolRegistry, limited
from study_coder.tools.skillload import skill_digest, skill_loader
from study_coder.types import AgentRunResult, Message, ToolCall, ToolResult, Usage


class Agent:
    """按轮次调用模型；每条工具请求都得到匹配的结果消息。"""

    def __init__(self, model: ChatModel, tools: ToolRegistry, permission: PermissionPolicy,
                 settings: Settings, runtime: WorkspaceRuntime, mode: Mode = "interactive",
                 context: ContextManager | None = None,
                 system_prompt: str = SYSTEM_PROMPT,
                 history_checkpoint: Callable[[list[Message]], None] | None = None,
                 model_activity: Callable[[bool], None] | None = None,
                 memory: MemoryManager | None = None) -> None:
        """注入模型、工具及策略，初始历史只含系统约定。"""
        self.model = model
        self.tools = tools
        self.permission = permission
        self.settings = settings
        self.mode = mode
        self.context = context or ContextManager(settings, ModelSummarizer(model))
        self.runtime = runtime
        self.system_prompt = system_prompt
        self.history_checkpoint = history_checkpoint
        self.model_activity = model_activity
        self.memory = memory
        if memory is not None:
            self.context.memory_projector = memory.projection
        self.history: list[Message] = [Message("system", system_prompt)]
        self.active_skills: dict[str, str] = {}
        self.total_usage = Usage()
        self.usage_available = False

    def clear(self) -> None:
        """清空对话和摘要缓存；累计用量及工作区文件不受影响。"""
        self.history = [Message("system", self.system_prompt)]
        self.context.reset()
        self.active_skills.clear()

    def _activate_skill(self, call: ToolCall, raw: ToolResult) -> ToolResult:
        """完整正文只进入活动状态；协议结果保留简短确认，失败不激活。"""
        if raw.is_error:
            return ToolResult(call.id, call.name,
                              limited(raw.content, self.settings.max_tool_output_chars), True)
        loader = self.tools.get("skill_loader")
        name = call.arguments.get("skill_name")
        if not isinstance(loader, skill_loader) or not isinstance(name, str) or name not in loader.skills:
            return ToolResult(call.id, call.name, "Error: skill is absent from the trusted catalog", True)
        body = loader.skills[name]["body"]
        if raw.content != body:
            return ToolResult(call.id, call.name, "Error: skill body differs from the image catalog; not activated", True)
        if len(body) > self.settings.max_skill_chars:
            return ToolResult(call.id, call.name, "Error: skill exceeds max_skill_chars; not activated", True)
        proposed = {**self.active_skills, name: body}
        if sum(map(len, proposed.values())) > self.settings.max_active_skill_chars:
            return ToolResult(call.id, call.name, "Error: active skills exceed max_active_skill_chars; not activated", True)
        schemas = self.tools.schemas()
        if self.context.pinned_tokens(self.system_prompt, proposed, schemas) >= 0.5 * self.settings.context_window_tokens:
            return ToolResult(call.id, call.name, "Error: skill and tool schemas leave insufficient context budget; not activated", True)
        already_active = name in self.active_skills
        # 此处不向 history 插入 system，当前轮剩余工具结果仍连续排列。
        self.active_skills = proposed
        content = f"Skill {name!r} is already active." if already_active else f"Activated skill {name!r} ({len(body)} characters)."
        return ToolResult(call.id, call.name, content)

    def skill_state(self) -> dict[str, str]:
        """会话仅保存名称和内容摘要；正文恢复自当前可信镜像清单。"""
        return {name: skill_digest(body) for name, body in self.active_skills.items()}

    def restore_session(self, history: list[Message], skills: dict[str, str],
                        memory_state: dict | None = None) -> None:
        """校验历史和已保存的技能哈希，从当前可信清单恢复正文。"""
        self.context.validate_tool_message_groups(history)
        loader = self.tools.get("skill_loader")
        restored: dict[str, str] = {}
        for name, digest in skills.items():
            if not isinstance(loader, skill_loader) or name not in loader.skills:
                raise ToolFailure(f"session skill {name!r} is unavailable in this image")
            body = loader.skills[name]["body"]
            if skill_digest(body) != digest:
                raise ToolFailure(f"session skill {name!r} changed; start a new session or use its original image")
            if len(body) > self.settings.max_skill_chars:
                raise ToolFailure(f"session skill {name!r} exceeds max_skill_chars")
            restored[name] = body
        if sum(map(len, restored.values())) > self.settings.max_active_skill_chars:
            raise ToolFailure("session active skills exceed max_active_skill_chars")
        if restored and self.context.pinned_tokens(self.system_prompt, restored, self.tools.schemas()) >= 0.5 * self.settings.context_window_tokens:
            raise ToolFailure("session active skills leave insufficient context budget")
        self.history = list(history)
        self.active_skills = restored
        self.context.restore_memory(memory_state, history)

    def _execute_call(self, call: ToolCall) -> ToolResult:
        """执行单个工具请求，把拒绝或失败变为模型可读取的观察。"""
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, f"Unknown tool: {call.name}", True)
        if not tool.is_read_only:
            # 只读工具直接执行；有副作用的工具先经过当前运行模式的授权策略。
            decision = self.permission.decide(PermissionRequest(call.name, call.arguments,
                                                                 self.mode))
            if decision == "deny":
                return ToolResult(call.id, call.name,
                                  f"{call.name} denied in {self.mode} mode", True)
        try:
            if call.name in MEMORY_TOOLS:
                if self.memory is None:
                    return ToolResult(call.id, call.name, "Error: project memory is disabled", True)
                raw = self.memory.execute(call.name, call.arguments, self.settings.max_tool_output_chars)
            elif call.name in SANDBOX_TOOLS:
                raw = self.runtime.execute_tool(call.name, call.arguments)

            elif call.name == "web_search":
                # 搜索仍是宿主工具；工作区与命令工具只能经 DockerRuntime。
                context = ToolContext(self.settings.workspace, self.settings.workspace,
                                      self.settings)
                raw = tool.execute(call.arguments, context)
            else:
                return ToolResult(call.id, call.name,
                                  "Error: tool has no Docker execution route", True)
        except KeyboardInterrupt:
            raise
        except Exception:
            # 工具层应自行解释预期输入错误；这里隐藏意外异常的内部细节。
            return ToolResult(call.id, call.name, "Internal tool error", True)
        if call.name == "skill_loader":
            return self._activate_skill(call, raw)
        return ToolResult(call.id, call.name,
                          limited(raw.content, self.settings.max_tool_output_chars), raw.is_error)

    @staticmethod
    def _log_tool_call(call: ToolCall) -> None:
        """在实际执行前显示工具请求；限制参数长度，避免刷满终端。"""
        # displayed 只影响终端预览；模型原参数保留 job_id，用于后续正确查询任务。
        displayed = {k: v for k, v in call.arguments.items()
                     if call.name not in BACKGROUND_TOOLS or k != "job_id"}
        arguments = limited(json.dumps(displayed, ensure_ascii=False, default=str), 50)
        arguments = arguments.replace("\n", "\\n")
        print(f"[工具调用] {json.dumps(call.name, ensure_ascii=False)} "
              f"参数={arguments}", file=sys.stderr, flush=True)

    def _record_tool_result(self, result: ToolResult, results: list[ToolResult]) -> None:
        """将真实结果加入本轮观察和历史，另构造不显示编号的终端预览。"""
        results.append(result)
        self.history.append(result.as_message)
        content = result.content  # 显示用局部副本；修改预览不改 history 中的协议内容。
        if result.name in BACKGROUND_TOOLS:
            try:
                value = json.loads(content)
                value.pop("job_id", None)
                for item in value.get("jobs", []):
                    item.pop("job_id", None)
                content = json.dumps(value, ensure_ascii=False)
            except (ValueError, AttributeError, TypeError):
                pass
            content = re.sub(r"job_[0-9a-f]{32}", "[background job]", content)
        preview = json.dumps(limited(content, 800), ensure_ascii=False)
        status = "失败" if result.is_error else "成功"
        print(f"[工具结果] {json.dumps(result.name, ensure_ascii=False)} "
              f"状态={status} 内容={preview}", file=sys.stderr, flush=True)

    def _commit_results(self, results: list[ToolResult]) -> None:
        """先保存完整调用组，后确认终态已交付；保存失败时不消耗后台结果。

        results 是本次 run 截至当前的累计工具观察；重复确认同一编号是幂等的。
        history_checkpoint 是 CLI 注入的保存回调，无回调的真实运行时使用默认保存。
        """
        if self.history_checkpoint is not None:
            self.history_checkpoint(self.history)
        # delivered 保存执行编号 job_id，而 result.call_id 对应一次模型工具调用。
        # 同一任务的 start/get/wait 可有多个真实 call_id，不能复用启动 call_id。
        delivered = []
        for result in results:
            if result.name not in {"get_background", "wait_background", "cancel_background"}:
                # 启动确认即使返回快速终态，也不能代替显式读取最终日志。
                continue
            try:
                value = json.loads(result.content)
                if value.get("status") in TERMINAL and isinstance(value.get("job_id"), str):
                    delivered.append(value["job_id"])
            except (ValueError, AttributeError, TypeError):
                continue
        acknowledge = getattr(self.runtime, "acknowledge_background", None)
        if acknowledge is not None:
            acknowledge(delivered)

    def _background_ids(self) -> set[str]:
        """获取当前沙箱全部登记编号；兼容未实现后台接口的测试运行时。"""
        getter = getattr(self.runtime, "background_job_ids", None)
        return getter() if getter is not None else set()

    def _interrupt_background(self, initial: set[str], wait_target: str | None = None) -> None:
        """停止本轮新建任务，另停止正在显式等待的目标；其他历史任务继续运行。

        initial 是本轮开始前的编号集合；wait_target 可是前一轮已启动的任务。
        """
        cancel = getattr(self.runtime, "interrupt_background", None)
        if cancel is not None:
            targets = self._background_ids() - initial  # 集合差：本轮创建的执行编号。
            if wait_target:
                targets.add(wait_target)
            cancel(targets)

    def run(self, user_text: str) -> AgentRunResult:
        """工作和短期层由上下文维护；一轮结束后归档可追溯的情景观察。"""
        start = len(self.history)
        directory = getattr(self.runtime, "directory", None)
        origin = f"task={directory.name if directory is not None else 'api'};history_start={start}"
        if self.memory is not None:
            self.memory.origin = origin
            self.memory.user_request = user_text
        try:
            outcome = self._run_turn(user_text)
            if self.memory is not None:
                try:
                    # 来源可定位到任务会话的消息区间；每次 run 分别归档历史观察。
                    self.memory.record_episode(user_text, outcome.tool_results, outcome.stop_reason,
                                               origin + f";history_end={len(self.history)};" + uuid.uuid4().hex)
                except (ValueError, OSError, sqlite3.Error) as exc:
                    print(f"[记忆] 情景归档失败：{exc}", file=sys.stderr, flush=True)
            return outcome
        finally:
            if self.memory is not None:
                # 一个用户请求结束后，旧原话不能再为后续独立写入提供来源证明。
                self.memory.user_request = None

    def _run_turn(self, user_text: str) -> AgentRunResult:
        """运行一个用户任务，直到得到最终答复、触及轮数上限或中断。

        history 保存完整原始消息；每轮传给模型的是 ContextManager 生成的压缩视图。
        一次模型响应中可能有多个工具调用，必须先追加 assistant 请求，再按顺序
        追加所有 tool 结果，下一轮才可把历史传回模型。
        """
        self.history.append(Message("user", user_text))
        results: list[ToolResult] = []  # 本次 run 的工具观察，跨模型轮次累计。
        # 每次 run 独立计数，不从会话历史推算，避免旧任务和补齐的结果被重复计入。
        tool_call_counts: dict[str, int] = {}
        prompt_tokens = completion_tokens = 0
        usage_available = False
        rounds = 0  # 已进入的模型轮次；后台 wait 的内部等待不增加它。
        memory_action = MemoryAction.from_request(user_text) if self.memory is not None else None
        memory_tools = {"memory_read", "memory_search", memory_action.tool} if memory_action else set()
        memory_action_state = ""
        publish: Callable[[str], None] | None = None

        def finish(text: str, extra_reasons: tuple[str, ...] = ()) -> AgentRunResult:
            """模型正文和宿主确认共用完成判定，仍核对任务、后台和交付条件。"""
            reasons = extra_reasons + (("tasks_incomplete",) if self.runtime.get_incomplete_tasks() else ())
            reasons += getattr(self.runtime, "get_background_pending", lambda: ())()
            export_issues = tuple(getattr(self.runtime, "get_export_issues", lambda: ())())
            if export_issues:
                reasons += ("workspace_unexportable",)
            stop: Literal["completed", "incomplete"] = "incomplete" if reasons else "completed"
            return AgentRunResult(text, stop, rounds, list(self.history), results,
                                  Usage(prompt_tokens, completion_tokens) if usage_available else None,
                                  tool_call_counts=dict(tool_call_counts), incomplete_reasons=reasons,
                                  export_issues=export_issues)

        initial_background = self._background_ids()  # 中断时保留此前任务的边界快照。
        try:
            for rounds in range(1, self.settings.max_rounds + 1):
                confirmation = (memory_action.confirmation(self.memory.store)
                                if memory_action and self.memory is not None else None)
                schemas = self.tools.schemas()
                if memory_action:
                    schemas = [item for item in schemas if item["function"]["name"] in memory_tools]
                # 状态只进入本轮模型视图，不写入历史，不交给旧对话摘要。
                task_state = self.runtime.get_task_state()
                # task_state 是 .tasks 的计划摘要，background_state 是命令执行元数据；
                # 两种状态机独立，命令成功不能自动把计划项标成 completed。
                background_state = getattr(self.runtime, "get_background_state", lambda: "")()
                memory_context = MemoryContext()
                if self.memory is not None:
                    try:
                        memory_context = self.memory.build_context(user_text + "\n" + task_state, self.context)
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        print(f"[记忆] 本轮跳过长期回忆：{exc}", file=sys.stderr, flush=True)
                model_messages = self.context.build_view(
                    self.history, task_state=task_state,
                    background_state=background_state,
                    active_skills=self.active_skills, tool_schemas=schemas,
                    memory_context=memory_context.related, core_memory_context=memory_context.core,
                    memory_action_state=memory_action_state)
                if self.context.last_summary_usage:
                    # 摘要模型也会消耗 token；仅有供应方报告时才累计。
                    summary_usage = self.context.last_summary_usage
                    usage_available = True
                    self.usage_available = True
                    prompt_tokens += summary_usage.prompt_tokens
                    completion_tokens += summary_usage.completion_tokens
                    self.total_usage = Usage(
                        self.total_usage.prompt_tokens + summary_usage.prompt_tokens,
                        self.total_usage.completion_tokens + summary_usage.completion_tokens,
                    )
                if self.model_activity is not None:
                    # CLI 借此暂停流式答复期间的通知；模型结束／异常都经 finally 恢复。
                    self.model_activity(True)
                try:
                    options = getattr(self.model, "response_options", None)
                    response_scope = (options(require_tool=not bool(confirmation), defer_text=True)
                                      if memory_action and options is not None else nullcontext(None))
                    with response_scope as publish:
                        reply = self.model.complete(model_messages, schemas)
                finally:
                    if self.model_activity is not None:
                        self.model_activity(False)
                if reply.assistant_message.role != "assistant" or tuple(reply.tool_calls) != reply.assistant_message.tool_calls:
                    # 阻止工具调用列表与历史中实际记录的 assistant 消息分叉。
                    raise ValueError("model reply and assistant message disagree")
                if reply.usage:
                    usage_available = True
                    self.usage_available = True
                    prompt_tokens += reply.usage.prompt_tokens
                    completion_tokens += reply.usage.completion_tokens
                    self.total_usage = Usage(
                        self.total_usage.prompt_tokens + reply.usage.prompt_tokens,
                        self.total_usage.completion_tokens + reply.usage.completion_tokens,
                    )
                if memory_action and reply.interrupted:
                    # 流式草稿尚未展示，截断时也不能把其中的“已保存”当作回执。
                    text = confirmation or memory_action.failure()
                    reply = replace(reply, text=text, assistant_message=Message("assistant", text))
                    if publish is not None:
                        publish(text)
                if memory_action and not reply.tool_calls and not reply.interrupted:
                    # 本轮完成不能由模型文字自证。重读数据库，回执还必须来自本轮
                    # 真正执行的修改工具；召回、查询和历史里的 saved=true 都不算。
                    confirmation = memory_action.confirmation(self.memory.store) if self.memory else None
                    if confirmation:
                        reply = replace(reply, text=confirmation, assistant_message=Message("assistant", confirmation))
                        if publish is not None:
                            publish(confirmation)
                    elif memory_action.can_retry and rounds < min(6, self.settings.max_rounds):
                        memory_action_state = memory_action.correction()
                        print(f"[记忆核验] 本轮操作尚未确认，要求模型调用工具（{memory_action.retries}/2）",
                              file=sys.stderr, flush=True)
                        # 被拒绝的草稿不加入可复用会话，避免虚构 ID 在下一轮继续传播。
                        continue
                    else:
                        break
                # 不把截断响应中的调用写入历史或执行；即使自定义模型绕过适配器，
                # 这里仍保证后续对话里没有悬空的工具调用。
                self.history.append(Message("assistant", reply.text)
                                    if reply.interrupted else reply.assistant_message)
                if reply.interrupted:
                    reason = {"length": "model_output_truncated",
                              "content_filter": "model_content_filtered"}.get(
                                  reply.finish_reason or "", "model_response_incomplete")
                    return AgentRunResult(reply.text, "incomplete", rounds, list(self.history),
                                          results, Usage(prompt_tokens, completion_tokens)
                                          if usage_available else None,
                                          tool_call_counts=dict(tool_call_counts),
                                          incomplete_reasons=(reason,))
                if not reply.tool_calls:
                    if reply.text.strip():
                        return finish(reply.text)
                    # 空文本也没有工具调用时，给模型一次明确的继续指令。
                    self.history.append(Message("system", "The previous assistant response was "
                                                "empty. Return a final answer or request a tool."))
                    continue
                for index, call in enumerate(reply.tool_calls):
                    # 在权限检查和执行前计入一次；中断后仅补齐结果的 pending 不走这里。
                    tool_call_counts[call.name] = tool_call_counts.get(call.name, 0) + 1
                    self._log_tool_call(call)
                    try:
                        result = (ToolResult(call.id, call.name,
                                  "Error: tool is not available for this memory management request", True)
                                  if memory_action and call.name not in memory_tools else self._execute_call(call))
                    except KeyboardInterrupt:
                        # 中断时为当前与尚未执行的调用补齐结果，避免留下孤立请求。
                        result = ToolResult(call.id, call.name, "Interrupted before tool completed", True)
                        self._record_tool_result(result, results)
                        for pending in reply.tool_calls[index + 1:]:
                            skipped = ToolResult(pending.id, pending.name,
                                                 "Skipped because run was interrupted", True)
                            self._record_tool_result(skipped, results)
                        # 用户中断等待旧任务也应停止该目标，而不是只取消本轮新任务。
                        wait_target = call.arguments.get("job_id") if call.name == "wait_background" else None
                        self._interrupt_background(initial_background, wait_target)
                        self._commit_results(results)
                        return AgentRunResult("Interrupted", "interrupted", rounds,
                                              list(self.history), results,
                                              Usage(prompt_tokens, completion_tokens)
                                              if usage_available else None,
                                              tool_call_counts=dict(tool_call_counts))
                    self._record_tool_result(result, results)
                    if memory_action is not None:
                        memory_action.observe(call, result)
                # 只在整组结果补齐后落盘，避免恢复到半个工具调用组。
                self._commit_results(results)
                if memory_action and memory_action.denied:
                    break  # 用户/运行模式已拒绝操作，不能再次要求模型重试授权。
                if memory_action and rounds >= 6:
                    # 记忆管理单独限次；查询空转或反复写入都不能耗尽开发任务的大预算。
                    break
            if memory_action is not None:
                # 最后一轮刚好完成写入时无需再向模型索要确认，宿主已有核验依据。
                confirmation = memory_action.confirmation(self.memory.store) if self.memory else None
                text = confirmation or memory_action.failure()
                self.history.append(Message("assistant", text))
                if confirmation and publish is not None:
                    publish(text)
                return finish(text, () if confirmation else ("memory_operation_unconfirmed",))
            return AgentRunResult("Reached maximum model rounds", "round_limit", rounds,
                                  list(self.history), results,
                                  Usage(prompt_tokens, completion_tokens)
                                  if usage_available else None,
                                  tool_call_counts=dict(tool_call_counts))
        except KeyboardInterrupt:
            # 模型请求或上下文构造阶段的中断没有待补齐的工具结果。
            self._interrupt_background(initial_background)
            return AgentRunResult("Interrupted", "interrupted", rounds, list(self.history),
                                  results, Usage(prompt_tokens, completion_tokens)
                                  if usage_available else None,
                                  tool_call_counts=dict(tool_call_counts))
        except Exception as exc:
            # 模型连接、协议解析及上下文校验失败都作为本次运行失败返回。
            return AgentRunResult(f"Model or protocol failure: {exc}", "failed", rounds,
                                  list(self.history), results,
                                  Usage(prompt_tokens, completion_tokens)
                                  if usage_available else None,
                                  tool_call_counts=dict(tool_call_counts))
