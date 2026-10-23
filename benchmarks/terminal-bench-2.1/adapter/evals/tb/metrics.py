"""汇总一个 Harbor job 目录的评测指标。

指标口径：
- Resolved Rate：verifier reward == 1.0 的 trial 占比。
- Token / Cost：Harbor 记录的输入/输出 token 与 cost_usd。
- Tool Calls：轨迹中每条工具结果的次数与成功率（is_error 视为失败）。
- Execution Time：trial 的 agent 执行时长，另附 verifier 与总时长。
- Error Recovery：失败调用之后是否出现成功调用，以及最长连续失败次数。

用法：``python -m evals.tb.metrics .cache/harbor-jobs/tb-sample10``
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

SESSION_PATH = Path("agent") / "coder-session.json"
# verifier 需要临时安装工具；这些标记说明评分环境本身没准备好，不是模型失败。
VERIFIER_INFRA_MARKERS = ("Failed to fetch", "Unable to locate package",
                          "uvx: command not found", "Temporary failure resolving")
# verifier 明确报连接失败时，优先归因于评测环境/服务能力，而非模型解法。
# 不用裸 “Connection refused”：浏览器驱动等背景日志也会出现该词。
SERVICE_MARKERS = ("requests.exceptions.ConnectionError",
                   "Max retries exceeded with url",
                   "failed to connect to all addresses", "is not listening",
                   "grpc._channel._InactiveRpcError")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _seconds(start: Any, end: Any) -> float | None:
    """计算两个 ISO 时间戳的秒数；缺失或格式错误时返回 None。"""
    if not start or not end:
        return None
    try:
        begin = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max((finish - begin).total_seconds(), 0.0)


def _recovery(results: list[dict[str, Any]]) -> dict[str, Any]:
    """统计失败事件及其后的恢复情况。"""
    failures = [index for index, item in enumerate(results) if item.get("is_error")]
    recovered = sum(1 for index in failures
                    if any(not results[later].get("is_error")
                           for later in range(index + 1, len(results))))
    longest = current = 0
    for item in results:
        current = current + 1 if item.get("is_error") else 0
        longest = max(longest, current)
    return {"failed_calls": len(failures), "recovered_failures": recovered,
            "longest_failure_streak": longest}


def _tool_stats(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for item in results:
        name = str(item.get("name") or "unknown")
        entry = stats.setdefault(name, {"calls": 0, "failures": 0})
        entry["calls"] += 1
        entry["failures"] += int(bool(item.get("is_error")))
    return stats


def _verifier_infra_failed(trial_dir: Path, resolved: bool) -> bool:
    """评分环境安装依赖失败时，trial 的 0 分不能归因给模型。"""
    if resolved:
        return False
    try:
        output = (trial_dir / "verifier" / "test-stdout.txt").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in output for marker in VERIFIER_INFRA_MARKERS)


def _verifier_service_failed(trial_dir: Path, resolved: bool,
                             stop_reason: Any) -> bool:
    """agent 已结束但 verifier 连不上本地服务时，属于适配器限制。"""
    if resolved or stop_reason != "completed":
        return False
    try:
        output = (trial_dir / "verifier" / "test-stdout.txt").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in output for marker in SERVICE_MARKERS)


def _classify(*, resolved: bool, adapter_error: bool, agent_timeout: bool,
              service_unavailable: bool, verifier_infra: bool) -> str:
    if resolved:
        return "resolved"
    if adapter_error:
        return "adapter"
    if agent_timeout:
        return "timeout"
    if service_unavailable:
        return "adapter-limited"
    if verifier_infra:
        return "verifier-infra"
    return "model"


def collect_trial(trial_dir: Path) -> dict[str, Any] | None:
    """读取单个 trial；尚未产出 result.json 时返回 None。"""
    result = _read_json(trial_dir / "result.json")
    if result is None:
        return None
    session = _read_json(trial_dir / SESSION_PATH) or {}
    agent_result = result.get("agent_result") or {}
    metadata = agent_result.get("metadata") or {}
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    results = session.get("tool_results") or []
    tool_stats = _tool_stats(results)
    calls = sum(entry["calls"] for entry in tool_stats.values())
    failures = sum(entry["failures"] for entry in tool_stats.values())
    recovery = _recovery(results)
    stop_reason = session.get("stop_reason") or metadata.get("stop_reason")
    exception = result.get("exception_info") or {}
    exception_type = exception.get("exception_type")
    resolved = reward == 1.0
    agent_timeout = exception_type == "AgentTimeoutError"
    adapter_error = bool(exception) and not agent_timeout
    verifier_infra = _verifier_infra_failed(trial_dir, resolved)
    service_unavailable = _verifier_service_failed(trial_dir, resolved, stop_reason)
    return {
        "trial": trial_dir.name,
        "task": result.get("task_name"),
        "reward": reward,
        "resolved": resolved,
        "stop_reason": stop_reason,
        "rounds": session.get("rounds", metadata.get("rounds")),
        "input_tokens": agent_result.get("n_input_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "cost_usd": agent_result.get("cost_usd"),
        "tool_calls": calls,
        "tool_failures": failures,
        "tool_success_rate": (calls - failures) / calls if calls else None,
        "tool_calls_by_name": tool_stats,
        "execution_seconds": _seconds((result.get("agent_execution") or {}).get("started_at"),
                                      (result.get("agent_execution") or {}).get("finished_at")),
        "verifier_seconds": _seconds((result.get("verifier") or {}).get("started_at"),
                                     (result.get("verifier") or {}).get("finished_at")),
        "total_seconds": _seconds(result.get("started_at"), result.get("finished_at")),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "failed_calls": recovery["failed_calls"],
        "recovered_failures": recovery["recovered_failures"],
        "longest_failure_streak": recovery["longest_failure_streak"],
        "failure_recovered": (recovery["failed_calls"] == 0
                              or recovery["recovered_failures"] > 0),
        "exception_type": exception_type,
        "adapter_error": adapter_error,
        "agent_timeout": agent_timeout,
        "verifier_infra_error": verifier_infra,
        "service_unavailable": service_unavailable,
        "classification": _classify(resolved=resolved, adapter_error=adapter_error,
                                    agent_timeout=agent_timeout,
                                    service_unavailable=service_unavailable,
                                    verifier_infra=verifier_infra),
        "has_trajectory": bool(session),
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _merge_tool_stats(trials: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for trial in trials:
        for name, entry in trial["tool_calls_by_name"].items():
            total = merged.setdefault(name, {"calls": 0, "failures": 0})
            total["calls"] += entry["calls"]
            total["failures"] += entry["failures"]
    return merged


def summarize(trials: list[dict[str, Any]]) -> dict[str, Any]:
    n_trials = len(trials)
    n_resolved = sum(int(trial["resolved"]) for trial in trials)
    calls = sum(trial["tool_calls"] for trial in trials)
    failures = sum(trial["tool_failures"] for trial in trials)
    failed_calls = sum(trial["failed_calls"] for trial in trials)
    recovered = sum(trial["recovered_failures"] for trial in trials)
    times = [trial["execution_seconds"] for trial in trials
             if trial["execution_seconds"] is not None]
    costs = [trial["cost_usd"] for trial in trials if trial["cost_usd"] is not None]
    failure_runs = [trial for trial in trials if trial["failed_calls"] > 0]
    verifier_infra = sum(int(trial["verifier_infra_error"]) for trial in trials)
    adapter_limited = sum(int(trial["service_unavailable"]) for trial in trials)
    scored = n_trials - verifier_infra - adapter_limited
    return {
        "n_trials": n_trials,
        "n_resolved": n_resolved,
        "resolved_rate": n_resolved / n_trials if n_trials else None,
        "resolved_rate_scored": n_resolved / scored if scored else None,
        "n_adapter_errors": sum(int(trial["adapter_error"]) for trial in trials),
        "n_adapter_limited": adapter_limited,
        "n_agent_timeouts": sum(int(trial["agent_timeout"]) for trial in trials),
        "n_verifier_infra_errors": verifier_infra,
        "n_model_failures": sum(int(trial["classification"] == "model") for trial in trials),
        "n_missing_trajectory": sum(int(not trial["has_trajectory"]) for trial in trials),
        "n_input_tokens": sum(trial["input_tokens"] or 0 for trial in trials),
        "n_output_tokens": sum(trial["output_tokens"] or 0 for trial in trials),
        "cost_usd": sum(costs) if costs else None,
        "tool_calls": calls,
        "tool_failures": failures,
        "tool_success_rate": (calls - failures) / calls if calls else None,
        "tool_calls_by_name": _merge_tool_stats(trials),
        "execution_seconds_mean": _mean(times),
        "execution_seconds_median": statistics.median(times) if times else None,
        "execution_seconds_total": sum(times) if times else None,
        "failure_runs": len(failure_runs),
        "failure_runs_recovered": sum(int(trial["failure_recovered"]) for trial in failure_runs),
        "failed_calls": failed_calls,
        "recovered_failures": recovered,
        "failure_recovery_rate": recovered / failed_calls if failed_calls else None,
        "longest_failure_streak": max((trial["longest_failure_streak"] for trial in trials),
                                      default=0),
    }


def collect(job_dir: Path) -> dict[str, Any]:
    trials = [trial for trial in (collect_trial(path) for path in sorted(job_dir.iterdir())
                                  if path.is_dir()) if trial is not None]
    return {"jobs": [str(job_dir.resolve())], "summary": summarize(trials), "trials": trials}


def collect_many(job_dirs: list[Path]) -> dict[str, Any]:
    """合并 job，每道任务按结束时间选择一条记录。"""
    latest: dict[str, dict[str, Any]] = {}
    for job_dir in job_dirs:
        if not job_dir.is_dir():
            continue
        for path in sorted(job_dir.iterdir()):
            if not path.is_dir():
                continue
            trial = collect_trial(path)
            if trial is None:
                continue
            key = str(trial["task"] or trial["trial"])
            previous = latest.get(key)
            if previous is None or str(trial["finished_at"] or "") >= str(
                    previous["finished_at"] or ""):
                latest[key] = trial
    trials = sorted(latest.values(), key=lambda item: str(item["task"]))
    return {"jobs": [str(job_dir.resolve()) for job_dir in job_dirs],
            "summary": summarize(trials), "trials": trials}


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    jobs = ", ".join(f"`{job}`" for job in report["jobs"])
    lines = ["# Evaluation Metrics", "",
             f"- Jobs: {jobs}",
             f"- Trials: {summary['n_trials']}",
             f"- Resolved Rate: **{_fmt(summary['resolved_rate'], 3)}** "
             f"({summary['n_resolved']}/{summary['n_trials']})",
             f"- Failed: {summary['n_trials'] - summary['n_resolved']}",
             f"- Agent timeouts: {summary['n_agent_timeouts']}; "
             f"adapter errors: {summary['n_adapter_errors']}; "
             f"model failures: {summary['n_model_failures']}",
             f"- Tokens: input {summary['n_input_tokens']:,}, "
             f"output {summary['n_output_tokens']:,}",
             f"- Tool Calls: {summary['tool_calls']} "
             f"(success {_fmt(summary['tool_success_rate'], 3)})",
             f"- Execution Time: mean {_fmt(summary['execution_seconds_mean'], 1, 's')}, "
             f"median {_fmt(summary['execution_seconds_median'], 1, 's')}, "
             f"total {_fmt(summary['execution_seconds_total'], 1, 's')}",
             f"- Error Recovery: {summary['recovered_failures']}/"
             f"{summary['failed_calls']} failed calls recovered "
             f"({_fmt(summary['failure_recovery_rate'], 3)}), "
             f"{summary['failure_runs_recovered']}/{summary['failure_runs']} runs",
             "", "## Tools", "", "| Tool | Calls | Failures | Success |",
             "| --- | ---: | ---: | ---: |"]
    for name, entry in sorted(summary["tool_calls_by_name"].items()):
        rate = (entry["calls"] - entry["failures"]) / entry["calls"] if entry["calls"] else None
        lines.append(f"| {name} | {entry['calls']} | {entry['failures']} | "
                     f"{_fmt(rate, 3)} |")
    lines += ["", "## Trials", "",
              "| Task | Resolved | Reward | Stop | Rounds | Tokens in/out | "
              "Calls | Success | Agent s | Recovered |",
              "| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    for trial in report["trials"]:
        lines.append(
            f"| {trial['task']} | {'yes' if trial['resolved'] else 'no'} | "
            f"{_fmt(trial['reward'])} | "
            f"{_fmt(trial['stop_reason'])} | "
            f"{_fmt(trial['rounds'])} | {_fmt(trial['input_tokens'])}/"
            f"{_fmt(trial['output_tokens'])} | "
            f"{trial['tool_calls']} | {_fmt(trial['tool_success_rate'], 3)} | "
            f"{_fmt(trial['execution_seconds'], 1)} | "
            f"{trial['recovered_failures']}/{trial['failed_calls']} |")
    return "\n".join(lines) + "\n"


def write_report(job_dirs: list[Path], out_dir: Path | None = None) -> dict[str, Any]:
    report = collect_many(job_dirs)
    target = out_dir or job_dirs[0]
    target.mkdir(parents=True, exist_ok=True)
    (target / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    (target / "metrics.md").write_text(markdown, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dirs", nargs="+", type=Path, help="Harbor job 输出目录")
    parser.add_argument("--out", type=Path, help="指标写入目录，默认第一个 job")
    args = parser.parse_args(argv)
    report = write_report(args.job_dirs, args.out)
    print(render_markdown(report))
    target = args.out or args.job_dirs[0]
    print(f"written: {target / 'metrics.json'}, {target / 'metrics.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
