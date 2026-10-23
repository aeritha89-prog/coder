"""核验当前适配器的轨迹文件名、通过率和报告表格。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.tb.metrics import collect, render_markdown


def test_report_counts_all_tasks_and_reads_coder_trajectory(tmp_path: Path) -> None:
    for name, reward in (("passed", 1.0), ("failed", 0.0), ("missing-reward", None)):
        trial = tmp_path / name
        (trial / "agent").mkdir(parents=True)
        (trial / "verifier").mkdir()
        result = {"task_name": name, "verifier_result": {"rewards": {"reward": reward}}}
        (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (trial / "agent" / "coder-session.json").write_text(json.dumps({
            "stop_reason": "completed", "tool_results": [
                {"name": "bash", "is_error": False},
            ],
        }), encoding="utf-8")
        (trial / "verifier" / "test-stdout.txt").write_text(
            "uvx: command not found" if reward == 0.0 else "", encoding="utf-8")

    report = collect(tmp_path)
    assert report["summary"]["n_trials"] == 3
    assert report["summary"]["n_resolved"] == 1
    assert report["summary"]["resolved_rate"] == pytest.approx(1 / 3)
    assert report["summary"]["tool_calls"] == 3

    markdown = render_markdown(report)
    assert "(1/3)" in markdown
    assert "- Failed: 2" in markdown
    assert "scored-only" not in markdown
    assert "Verifier infra errors" not in markdown
    assert "Cost" not in markdown
    # 逐题表格在去掉 Class 和 Cost 列后，表头、分隔行与数据列数仍需一致。
    rows = [line for line in markdown.split("## Trials", 1)[1].splitlines()
            if line.startswith("|")]
    assert len(rows) == 5
    assert all(row.count("|") == 11 for row in rows)
