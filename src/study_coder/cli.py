"""命令行入口：构建 Agent、恢复会话并运行持续对话。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

from study_coder.agent import Agent
from study_coder.config import load_settings
from study_coder.context import ContextManager, ModelSummarizer
from study_coder.llm import OpenAICompatibleModel
from study_coder.memory import LOAD_POLICIES, MEMORY_KINDS, MemoryManager, MemoryStore
from study_coder.permissions import Decision, DefaultPermissionPolicy, PermissionRequest
from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.docker import (
    DockerRuntime,
    apply_task,
    build_image,
    cleanup_task,
    doctor,
    policy_for_task,
    recover_task,
    review_task,
)
from study_coder.sandbox.manifest import list_records, load_record
from study_coder.sandbox.policy import load_policy
from study_coder.session import load_session_bundle, save_session, session_path
from study_coder.tools import default_tools
from study_coder.types import Message


def _ask(request: PermissionRequest) -> Decision:
    """将用户输入映射到按次允许、当前会话允许或拒绝。"""
    print(f"Allow {request.tool_name} {request.arguments}? [y] once / [a] session / [n] no")
    answer = input("> ").strip().lower()
    if answer == "y":
        return "allow_once"
    if answer == "a":
        return "allow_session"
    return "deny"


def parser() -> argparse.ArgumentParser:
    """声明运行参数；配置文件中的默认值由 load_settings 提供。"""
    result = argparse.ArgumentParser(prog="study-coder", epilog="Management: study-coder memory --help; study-coder sandbox --help")
    result.add_argument("-C", "--workspace", type=Path, help="existing workspace directory")
    result.add_argument("--yes", action="store_true", help="preapprove side-effect tools")
    result.add_argument("--model")
    result.add_argument("--base-url")
    result.add_argument("--max-rounds", type=int)
    result.add_argument("--command-timeout", type=int)
    result.add_argument("--session", help="save or resume this named session")
    result.add_argument("--stream", action="store_true", help="show model text as it arrives")
    result.add_argument("--sandbox", choices=("docker",),
                        help="Docker execution (the only supported backend)")
    result.add_argument("--sandbox-profile", help="trusted task image profile (currently general)")
    result.add_argument("--sandbox-apply", choices=("review", "auto"),
                        help="review exported changes or apply an explicitly preauthorized export")
    return result


def _sandbox_command(argv: Sequence[str]) -> int:
    """只操作本项目清单中的任务；doctor 和 build 不启动 Agent。"""
    command = argparse.ArgumentParser(prog="study-coder sandbox")
    command.add_argument("action", choices=("doctor", "build", "list", "review", "apply",
                                               "export", "recover", "cleanup"))
    command.add_argument("task_id", nargs="?")
    command.add_argument("--profile", default="general")
    command.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                         help="append an export exclusion; repeat for multiple rules")
    command.add_argument("--discard", action="store_true",
                         help="discard unexported task data while cleaning resources")
    args = command.parse_args(argv)
    if args.exclude and args.action != "export":
        command.error("--exclude is only supported for sandbox export")
    try:
        policy = replace(load_policy(), profile=args.profile)
        if args.action == "doctor":
            info = doctor(policy)
            print(f"Docker Linux ready; image={info['image']} ({info['image_id']})")
        elif args.action == "build":
            build_image(policy)
            print(f"Sandbox image built: {policy.image}")
        elif args.action == "list":
            for record in list_records(policy.state_dir):
                print(f"{record['task_id']}  {record.get('status', 'unknown')}  "
                      f"{record.get('workspace', '')}")
        else:
            if not args.task_id:
                raise SandboxError("sandbox task ID is required")
            record = load_record(policy.state_dir, args.task_id)
            if args.action == "review":
                print(review_task(policy, args.task_id))
            elif args.action == "apply":
                diff = apply_task(policy, args.task_id)
                print(f"Applied: {sum(len(paths) for paths in diff.values())} changed files")
            elif args.action == "recover":
                recover_task(policy, args.task_id)
                print(f"Recovered task {args.task_id}; workspace changes were rolled back")
            elif args.action == "cleanup":
                status = cleanup_task(policy, args.task_id, discard=args.discard)
                print(f"Cleaned task {args.task_id}; status={status}")
            else:
                settings = replace(load_settings(), workspace=Path(record["workspace"]))
                runtime = DockerRuntime(settings, policy_for_task(policy, record), record)
                diff = runtime.export_changes(extra_exclude=tuple(args.exclude))
                print(f"Exported task {args.task_id}: {sum(map(len, diff.values()))} changes")
        return 0
    except (SandboxError, ValueError, OSError) as exc:
        print(f"Sandbox error: {exc}", file=sys.stderr)
        return 2


def _memory_command(argv: Sequence[str]) -> int:
    """直接管理项目长期层；无需启动 Docker 或请求模型。"""
    command = argparse.ArgumentParser(prog="study-coder memory")
    command.add_argument("action", choices=("list", "search", "remember", "forget"))
    command.add_argument("value", nargs="?", help="search query or memory id")
    command.add_argument("-C", "--workspace", type=Path)
    command.add_argument("--kind", choices=MEMORY_KINDS)
    command.add_argument("--key")
    command.add_argument("--content")
    command.add_argument("--load-policy", choices=LOAD_POLICIES)
    command.add_argument("--priority", type=int, help="always memory priority, 0..100 (default 50)")
    command.add_argument("--limit", type=int, default=20)
    args = command.parse_args(argv)
    try:
        settings = load_settings()
        settings = replace(settings, workspace=args.workspace or settings.workspace)
        store = MemoryStore(load_policy().state_dir, settings.workspace, settings.memory_max_entries)
        value: Any
        if args.action in {"list", "search"}:
            if args.action == "search" and not args.value:
                command.error("search requires a query")
            value = [asdict(entry) for entry in store.search(
                args.value or "", kind=args.kind, limit=args.limit, load_policy=args.load_policy)]
        elif args.action == "remember":
            if not args.kind or not args.key or not args.content:
                command.error("remember requires --kind, --key and --content")
            value = asdict(store.remember(args.kind, args.key, args.content,
                                         load_policy=args.load_policy, priority=args.priority))
        else:
            value = {"deleted": store.forget(args.value)}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, SandboxError) as exc:
        print(f"Memory error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    """装配依赖并进入对话；用户退出后统一保存和导出成果。"""
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] == "sandbox":
        return _sandbox_command(raw[1:])
    if raw and raw[0] == "memory":
        return _memory_command(raw[1:])
    args = parser().parse_args(raw)
    runtime: DockerRuntime | None = None
    policy_config = None
    try:
        settings = load_settings()
        settings = replace(settings, workspace=args.workspace or settings.workspace,
                           model=args.model or settings.model,
                           base_url=args.base_url or settings.base_url,
                           max_rounds=args.max_rounds if args.max_rounds is not None else settings.max_rounds,
                           command_timeout_s=args.command_timeout if args.command_timeout is not None else settings.command_timeout_s)
        # dataclasses.replace 会重新调用 Settings.__post_init__，验证命令行覆盖后的值。
        policy_config = load_policy()
        policy_config = replace(policy_config,
                                profile=args.sandbox_profile or policy_config.profile,
                                apply=args.sandbox_apply or policy_config.apply)
        runtime = DockerRuntime.start(settings, policy_config)
        background = getattr(runtime, "background", None)  # 当前沙箱的宿主管理器，控制终端通知。

        def ask_permission(request: PermissionRequest) -> Decision:
            """权限询问期间暂存通知，用户回答后再展示，避免遮盖授权提示。"""
            if background is not None:
                background.pause_notifications(True)
            try:
                return _ask(request)
            finally:
                if background is not None:
                    background.pause_notifications(False)

        def model_activity(busy: bool) -> None:
            """busy 表示一次模型请求进行中；仅流式模式需要暂停通知显示。"""
            # 完成通知不能混入一条流式答复；等待工具期间仍可显示心跳和结果。
            if background is not None and args.stream:
                if not busy:
                    # 流式文本可能没有尾换行；先结束当前行，再释放排队通知。
                    print(flush=True)
                background.pause_notifications(busy)
        session_root = policy_config.state_dir / "history"
        session_root.mkdir(parents=True, exist_ok=True)
        model = OpenAICompatibleModel(settings, stream=args.stream,
                                      on_text=lambda part: print(part, end="", flush=True))
        # 流式输出只用于用户可见答复；摘要需要一次完整的非流式回复。
        summary_model = OpenAICompatibleModel(settings) if args.stream else model
        context = ContextManager(settings, ModelSummarizer(summary_model))
        memory = (MemoryManager(MemoryStore(runtime.directory.parent, settings.workspace,
                                           settings.memory_max_entries),
                                settings.memory_max_context_tokens, settings.memory_top_k,
                                 settings.memory_auto_episode,
                                 always_max_context_tokens=settings.memory_always_max_context_tokens,
                                 always_max_entries=settings.memory_always_max_entries)
                  if settings.memory_enabled else None)
        policy = DefaultPermissionPolicy(yes=args.yes, ask=ask_permission)

        def checkpoint(history: list[Message]) -> None:
            """整组 tool 结果补齐后保存；成功返回后 Agent 才确认后台结果已读取。"""
            save_session(runtime.directory, "history", settings, history, agent.skill_state(),
                         context.memory_state())

        agent = Agent(model, default_tools(
            allow_web_search=policy_config.allow_web_search,
            service_ports=policy_config.service_ports,
            background_enabled=policy_config.background_enabled,
            memory_enabled=memory is not None,
            skill_catalog=runtime.skill_catalog),
            policy, settings, runtime=runtime,
                      mode="interactive", context=context,
                      history_checkpoint=checkpoint, model_activity=model_activity, memory=memory)
        if args.session and session_path(session_root, args.session).exists():
            # 原始历史与短期覆盖边界一起恢复；不恢复旧进程和上次授权。
            history, skills, memory_state = load_session_bundle(session_root, args.session, settings)
            agent.restore_session(history, skills, memory_state)
    except (ValueError, OSError, KeyError, SandboxError) as exc:
        if runtime is not None:
            try:
                runtime.export_changes()
            except (SandboxError, OSError):
                pass
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    def run(text: str) -> int:
        """复用同一 Agent 执行一次输入，并在指定会话时保存完整历史。"""
        outcome = agent.run(text)  # 当前用户输入的结果；交互模式返回后不关闭沙箱。
        if args.stream:
            print(flush=True)
        if not args.stream or outcome.stop_reason not in ("completed", "incomplete"):
            print(outcome.text, flush=True)
        # 先完成答复展示，再把本次任务的计数写到 stderr，与已有工具日志保持一致。
        print(f"[工具统计] 总调用次数={sum(outcome.tool_call_counts.values())}",
              file=sys.stderr, flush=True)
        for name, count in sorted(outcome.tool_call_counts.items()):
            print(f"[工具统计] {json.dumps(name, ensure_ascii=False)} 次数={count}",
                  file=sys.stderr, flush=True)
        if outcome.stop_reason == "incomplete":
            # descriptions 只翻译宿主判定的原因；不能靠模型最终答复判断后台完成。
            descriptions = {"tasks_incomplete": "计划仍有未完成事项",
                            "background_running": "后台命令仍在运行",
                            "background_unobserved": "后台结果尚未交付",
                            "background_unknown": "后台状态尚未确认",
                            "model_output_truncated": "模型答复因长度限制被截断，可继续对话",
                            "model_content_filtered": "模型答复被内容过滤中断",
                            "model_response_incomplete": "模型响应未完整结束",
                            "workspace_unexportable": "工作区包含无法交付的文件"}
            reasons = outcome.incomplete_reasons or ("tasks_incomplete",)
            print("[任务] " + "；".join(descriptions.get(r, r) for r in reasons)
                  + "，本轮已结束。", file=sys.stderr, flush=True)
            for issue in outcome.export_issues:
                print("[交付检查] " + issue, file=sys.stderr, flush=True)
        try:
            if args.session:
                save_session(session_root, args.session, settings, agent.history, agent.skill_state(),
                             context.memory_state())
            save_session(runtime.directory, "history", settings, agent.history, agent.skill_state(),
                         context.memory_state())
        except OSError as exc:
            print(f"Session save failed: {exc}", file=sys.stderr)
            return 1
        return 0 if outcome.stop_reason == "completed" else 1

    def finish(code: int) -> int:
        """CLI 真正退出时统一收尾；code 是本次进程的退出码，0 成功、非零未完成。"""
        if runtime is None:
            return code
        try:
            background = getattr(runtime, "background", None)
            if background is not None and background.pending():
                # 即使模型说已完成，只要任务仍运行或结果未交付，就不能以 0 退出。
                code = 1
                print("[后台任务] 退出前收集结果并停止未完成命令。", file=sys.stderr, flush=True)
            # export_changes 内部先取消／归档后台任务，再停容器并生成稳定文件差异。
            diff = runtime.export_changes()
            task_id = runtime.record["task_id"]
            print(f"[沙箱] 任务 {task_id}：新增 {len(diff['added'])}，"
                  f"修改 {len(diff['modified'])}，删除 {len(diff['deleted'])}")
            if policy_config is not None and policy_config.apply == "auto":
                apply_task(policy_config, task_id)
                print("[沙箱] 已按预授权策略应用导出文件")
            else:
                print(f"审阅：study-coder sandbox review {task_id}")
                print(f"应用：study-coder sandbox apply {task_id}")
            return code
        except (SandboxError, OSError) as exc:
            print(f"Sandbox export failed: {exc}", file=sys.stderr)
            return 1

    # 每轮只结束当前任务，不关闭容器；最后一轮的结果参与对话退出码。
    last_code = 0
    print(f"study-coder in {settings.workspace} | /help for commands")
    while True:
        try:
            line = input("study-coder> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return finish(last_code)
        if line in {"exit", "quit"}:
            return finish(last_code)
        if line == "/help":
            print("/help  /clear (working/short-term only)  /tokens  /memory [query]  /forget ID  exit")
        elif line == "/clear":
            agent.clear()
            print("Conversation cleared; project files were not changed.")
        elif line == "/tokens":
            if agent.usage_available:
                usage = agent.total_usage
                print(f"prompt: {usage.prompt_tokens}; completion: {usage.completion_tokens}")
            else:
                print("Token usage unavailable from this model so far.")
        elif line == "/memory" or line.startswith("/memory ") or line.startswith("/forget "):
            if memory is None:
                print("Project memory is disabled.")
                continue
            try:
                if line.startswith("/forget "):
                    print(json.dumps({"deleted": memory.store.forget(line[8:].strip())}))
                else:
                    print(json.dumps([asdict(entry) for entry in memory.store.search(line[7:].strip())],
                                     ensure_ascii=False, indent=2))
            except (ValueError, OSError, sqlite3.Error) as exc:
                print(f"Memory error: {exc}", file=sys.stderr)
        elif line:
            last_code = run(line)
