"""把 coder 的一次运行结果回填到 Harbor 的 AgentContext。

Harbor 只读 ``n_input_tokens`` / ``n_output_tokens`` / ``metadata`` 这些字段；
完整轨迹另写一份 JSON 到 trial 的 agent 日志目录，便于失败时区分模型未完成
与适配器异常。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from harbor.models.agent.context import AgentContext, ModelUsage

from coder.types import AgentRunResult

TRAJECTORY_NAME = "coder-session.json"


def apply_outcome(context: AgentContext, outcome: AgentRunResult, model: str,
                  notes: dict[str, Any] | None = None) -> None:
    """用量只在供应方报告时填写；其余字段始终记录停止原因。"""
    usage = outcome.usage
    if usage is not None:
        context.n_input_tokens = usage.prompt_tokens
        context.n_output_tokens = usage.completion_tokens
        context.model_usage = {model: ModelUsage(n_input_tokens=usage.prompt_tokens,
                                                n_output_tokens=usage.completion_tokens)}
    context.metadata = {
        **(notes or {}),
        "model": model,
        "stop_reason": outcome.stop_reason,
        "rounds": outcome.rounds,
        "tool_call_counts": dict(outcome.tool_call_counts),
        "incomplete_reasons": list(outcome.incomplete_reasons),
        "export_issues": list(outcome.export_issues),
        "trajectory": TRAJECTORY_NAME,
    }


def write_trajectory(logs_dir: Path, outcome: AgentRunResult,
                    model: str) -> Path:
    """原子写入完整历史；日志目录由 Harbor 创建。"""
    target = logs_dir / TRAJECTORY_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, **outcome.to_dict()}
    descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target
