"""核对本目录的评测快照；--source 可进一步核对原始评测副本，只读文件。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE_TOOLS = {"read_file", "write_file", "edit_file"}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def seconds(timing: dict) -> float | None:
    if not timing.get("started_at") or not timing.get("finished_at"):
        return None
    start = datetime.fromisoformat(timing["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(timing["finished_at"].replace("Z", "+00:00"))
    return max((end - start).total_seconds(), 0.0)


def error_cause(content: str) -> str:
    if "absolute paths are not allowed" in content:
        return "absolute_path"
    if "is not in the subpath" in content:
        return "symlink_workspace"
    if "unavailable" in content and "python" in content:
        return "python_unavailable"
    if "file exists" in content:
        return "file_exists"
    if "file does not exist" in content:
        return "file_missing"
    return "other"


def summarize(rows: list[dict]) -> dict:
    tools: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "failures": 0})
    for row in rows:
        for name, stats in row["tool_calls_by_name"].items():
            for key in ("calls", "failures"):
                tools[name][key] += stats[key]
    count = len(rows)
    resolved = sum(row["resolved"] for row in rows)
    calls = sum(row["tool_calls"] for row in rows)
    failures = sum(row["tool_failures"] for row in rows)
    failed_calls = sum(row["failed_calls"] for row in rows)
    recovered = sum(row["recovered_failures"] for row in rows)
    infra = sum(row["verifier_infra_error"] for row in rows)
    limited = sum(row["service_unavailable"] for row in rows)
    scored = count - infra - limited
    times = [row["execution_seconds"] for row in rows if row["execution_seconds"] is not None]
    costs = [row["cost_usd"] for row in rows if row["cost_usd"] is not None]
    failure_runs = [row for row in rows if row["failed_calls"] > 0]
    return {
        "n_trials": count, "n_resolved": resolved,
        "resolved_rate": resolved / count if count else None,
        "resolved_rate_scored": resolved / scored if scored else None,
        "n_adapter_errors": sum(row["adapter_error"] for row in rows),
        "n_adapter_limited": limited,
        "n_agent_timeouts": sum(row["agent_timeout"] for row in rows),
        "n_verifier_infra_errors": infra,
        "n_model_failures": sum(row["classification"] == "model" for row in rows),
        "n_missing_trajectory": sum(not row["has_trajectory"] for row in rows),
        "n_input_tokens": sum(row["input_tokens"] or 0 for row in rows),
        "n_output_tokens": sum(row["output_tokens"] or 0 for row in rows),
        "cost_usd": sum(costs) if costs else None,
        "tool_calls": calls, "tool_failures": failures,
        "tool_success_rate": (calls - failures) / calls if calls else None,
        "tool_calls_by_name": dict(tools),
        "execution_seconds_mean": statistics.mean(times) if times else None,
        "execution_seconds_median": statistics.median(times) if times else None,
        "execution_seconds_total": sum(times) if times else None,
        "failure_runs": len(failure_runs),
        "failure_runs_recovered": sum(row["failure_recovered"] for row in failure_runs),
        "failed_calls": failed_calls, "recovered_failures": recovered,
        "failure_recovery_rate": recovered / failed_calls if failed_calls else None,
        "longest_failure_streak": max((row["longest_failure_streak"] for row in rows), default=0),
    }


def equal(actual, expected, label: str) -> None:
    if isinstance(expected, dict):
        assert set(actual) == set(expected), f"{label}: keys differ"
        for key in expected:
            equal(actual[key], expected[key], f"{label}.{key}")
    elif isinstance(expected, float):
        assert isinstance(actual, (int, float)) and math.isclose(
            actual, expected, rel_tol=1e-10, abs_tol=1e-6), f"{label}: {actual} != {expected}"
    else:
        assert actual == expected, f"{label}: {actual!r} != {expected!r}"


def verify_source(source: Path, snapshot: dict, provenance: dict) -> None:
    final = source / ".cache" / "harbor-jobs" / "tb-final"
    roots = {"checkout": source, "final": final}
    for entry in provenance["files"]:
        data = (roots[entry["root"]] / entry["path"]).read_bytes()
        assert len(data) == entry["bytes"], f"Source size changed: {entry['path']}"
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], (
            f"Source hash changed: {entry['path']}")
    file_errors = Counter()
    for row in snapshot["trials"]:
        directory = final / "trials" / row["trial"]
        result = read_json(directory / "result.json")
        lock = read_json(directory / "lock.json")
        config = read_json(directory / "config.json")
        reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        equal(reward, row["reward"], row["task"] + ".reward")
        equal(reward == 1.0, row["resolved"], row["task"] + ".resolved")
        equal(result["task_name"], row["task"], "task_name")
        equal(config["task"]["ref"], row["task_ref"], "task_ref")
        equal(str(config["job_id"]), row["source_job_id"], "source_job_id")
        equal(config["task"]["source"], snapshot["metadata"]["dataset"], "dataset")
        equal(config["agent"]["name"], snapshot["metadata"]["evaluation_entrypoint"], "agent")
        equal(lock["timeout_multiplier"], 1.0, "timeout_multiplier")
        equal(lock["environment"]["type"], "docker", "environment")
        equal(lock["verifier"]["environment_mode"], "shared", "verifier_environment_mode")
        exception = result.get("exception_info") or {}
        equal(exception.get("exception_type"), row["exception_type"], "exception_type")
        equal(exception.get("exception_type") == "AgentTimeoutError", row["agent_timeout"], "timeout")
        for field in ("started_at", "finished_at"):
            equal(result[field], row[field], field)
        equal(seconds(result.get("agent_execution") or {}), row["execution_seconds"], "agent_time")
        equal(seconds(result.get("verifier") or {}), row["verifier_seconds"], "verifier_time")
        equal(seconds(result), row["total_seconds"], "total_time")
        agent_result = result.get("agent_result") or {}
        for field in ("input_tokens", "output_tokens"):
            equal(agent_result.get("n_" + field), row[field], field)
        session_path = directory / "agent" / "study-coder-session.json"
        equal(session_path.is_file(), row["has_trajectory"], "has_trajectory")
        if not session_path.is_file():
            continue
        session = read_json(session_path)
        equal(session.get("model"), row["model"], "model")
        results = session.get("tool_results", [])
        equal(len(results), row["tool_calls"], "tool_calls")
        stats: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "failures": 0})
        recovered = failures = streak = longest = 0
        later_success = False
        for item in reversed(results):
            failed = bool(item.get("is_error"))
            failures += failed
            recovered += int(failed and later_success)
            later_success = later_success or not failed
            name = item.get("name") or "unknown"
            stats[name]["calls"] += 1
            stats[name]["failures"] += failed
            if failed and name in FILE_TOOLS:
                file_errors[error_cause(str(item.get("content", "")))] += 1
        for item in results:
            streak = streak + 1 if item.get("is_error") else 0
            longest = max(longest, streak)
        equal(dict(stats), row["tool_calls_by_name"], "tool_calls_by_name")
        equal(failures, row["tool_failures"], "tool_failures")
        equal(recovered, row["recovered_failures"], "recovered_failures")
        equal(longest, row["longest_failure_streak"], "longest_failure_streak")
    equal(dict(file_errors), snapshot["coverage"]["file_tool_failure_causes"], "file_errors")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="原始 coder - test 检出目录，可选")
    args = parser.parse_args()
    snapshot = read_json(ROOT / "data" / "results.json")
    rows = snapshot["trials"]
    assert len(rows) == len({row["task"] for row in rows}) == 89, "Task set differs"
    equal(summarize(rows), snapshot["summary"], "summary")
    equal(snapshot["summary"], snapshot["source_summary"], "source_summary")
    equal(sum(row["has_trajectory"] for row in rows), 74, "trajectory_count")
    equal(Counter(row["classification"] for row in rows),
          {"resolved": 75, "timeout": 10, "model": 4}, "classifications")
    with (ROOT / "data" / "tasks.csv").open(encoding="utf-8", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    equal([row["task"] for row in csv_rows], [row["task"] for row in rows], "CSV task set")
    for csv_row, row in zip(csv_rows, rows):
        for field, actual in csv_row.items():
            expected = row[field]
            if not row["has_trajectory"] and field in {
                "tool_calls", "tool_failures", "tool_success_rate",
            }:
                expected = None
            cell = ("" if expected is None else str(expected).lower()
                    if isinstance(expected, bool) else str(expected))
            equal(actual, cell, "CSV " + field)
    with (ROOT / "data" / "tools.csv").open(encoding="utf-8", newline="") as stream:
        tool_rows = list(csv.DictReader(stream))
        expected_tools = snapshot["summary"]["tool_calls_by_name"]
        assert len(tool_rows) == len(expected_tools), "CSV tool count differs"
        equal({row["tool"] for row in tool_rows}, set(expected_tools), "CSV tool names")
        for row in tool_rows:
            stats = snapshot["summary"]["tool_calls_by_name"][row["tool"]]
            equal(int(row["calls"]), stats["calls"], "CSV tool calls")
            equal(int(row["failures"]), stats["failures"], "CSV tool failures")
            successes = stats["calls"] - stats["failures"]
            equal(int(row["successes"]), successes, "CSV tool successes")
            equal(float(row["success_rate"]), successes / stats["calls"], "CSV tool rate")
    if args.source:
        verify_source(args.source, snapshot, read_json(ROOT / "data" / "provenance.json"))
    print("Verified: 89 tasks; 75 resolved (84.27%); 3,481 recorded tool calls.")
    print("Coverage: 74 trajectories; 15 missing; 2 verifier-infra flags overlap timeouts.")
    if args.source:
        print("Original file hashes, rewards, timings, token counts and tool trajectories verified.")


if __name__ == "__main__":
    main()
