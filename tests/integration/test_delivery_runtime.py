"""真实 Docker 验证缓存卷、超大文件预检和硬链接交付。"""

import json
import shutil
import subprocess

import pytest

from study_coder.config import Settings
from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.docker import DockerRuntime, apply_task, cleanup_task, doctor
from study_coder.sandbox.policy import SandboxPolicy


def test_cache_isolation_and_export_preflight_with_real_container(tmp_path):
    policy = SandboxPolicy(state_dir=tmp_path / "state")
    try:
        doctor(policy)
    except SandboxError as exc:
        pytest.skip(f"Docker delivery image unavailable: {exc}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = DockerRuntime.start(Settings("test", None, None, workspace), policy)
    try:
        prepared = runtime.execute_tool("bash", {"command": (
            'test "$HOME" = /home/agent && '
            'test "$CARGO_HOME" = /home/agent/.cargo && '
            'test "$GRADLE_USER_HOME" = /home/agent/.gradle && '
            'mkdir -p "$CARGO_HOME" "$GRADLE_USER_HOME" "$GOPATH" && '
            'printf cache > "$CARGO_HOME/cache-canary" && '
            'printf cache > "$GRADLE_USER_HOME/cache-canary" && '
            'printf project > artifact.txt && ln artifact.txt hardlink.txt'
        )})
        assert not prepared.is_error, prepared.content
        assert runtime.get_export_issues() == []
        # 使用稀疏文件跨过真实 128 MiB 门槛，避免测试占用同等物理磁盘空间。
        large = runtime.execute_tool("bash", {"command": (
            "python -c \"from pathlib import Path; "
            "f=Path('large.bin').open('wb'); f.truncate(128*1024*1024+1); f.close()\""
        )})
        assert large.is_error and "large.bin" in large.content
        assert "exit code: 0" in large.content
        with pytest.raises(SandboxError, match="remains running"):
            runtime.export_changes()
        assert runtime.running and any("large.bin" in issue for issue in runtime.get_export_issues())
        repaired = runtime.execute_tool("bash", {"command": "rm large.bin"})
        assert not repaired.is_error, repaired.content

        # 后台快速退出也必须检查最终文件，不能只依赖 0.5 秒周期扫描。
        started = runtime.execute_tool("start_background", {"command": (
            "python -c \"f=open('background.bin','wb'); "
            "f.truncate(128*1024*1024+1); f.close()\""
        )})
        assert not started.is_error, started.content
        identifier = json.loads(started.content)["job_id"]
        ended = runtime.execute_tool("wait_background", {"job_id": identifier})
        record = json.loads(ended.content)
        assert ended.is_error and record["status"] == "failed" and record["exit_code"] == 0
        assert "workspace_unexportable" in record["reason"]
        assert "background.bin" in record["output_tail"]

        # 用户显式排除后重试；硬链接被物化为两份普通文件，HOME 缓存不进入差异。
        diff = runtime.export_changes(extra_exclude=("background.bin",))
        assert set(diff["added"]) == {"artifact.txt", "hardlink.txt"}
        apply_task(policy, runtime.record["task_id"])
        assert (workspace / "artifact.txt").read_bytes() == b"project"
        assert (workspace / "hardlink.txt").read_bytes() == b"project"
        assert not (workspace / ".cargo").exists() and not (workspace / ".gradle").exists()
        inspected = subprocess.run([shutil.which("docker"), "volume", "inspect", runtime.home_volume],
                                   capture_output=True, timeout=15, check=False)
        assert inspected.returncode != 0, "task HOME volume was not cleaned"
    finally:
        cleanup_task(policy, runtime.record["task_id"], discard=True)
