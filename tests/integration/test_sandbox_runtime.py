"""真实 Docker 边界验收；本机无 Docker 或镜像时明确跳过。"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from study_coder.agent import Agent
from study_coder.config import Settings
from study_coder.llm import ScriptedLLM
from study_coder.permissions import DefaultPermissionPolicy
from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.docker import DockerRuntime, apply_task, doctor
from study_coder.sandbox.policy import SandboxPolicy
from study_coder.tools import default_tools
from study_coder.tools.skillload import skill_loader
from study_coder.types import LLMReply, Message, ToolCall


def test_real_container_tools_and_reviewed_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    policy = SandboxPolicy(state_dir=tmp_path / "state")
    try:
        doctor(policy)
    except SandboxError as exc:
        pytest.skip(f"Docker sandbox image is unavailable: {exc}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("old\n", encoding="utf-8")
    (workspace / ".env").write_text("FAKE_SECRET=host-canary\n", encoding="utf-8")
    (workspace / ".env.example").write_text("FAKE_SECRET=\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_SECRET", "host-canary")
    settings = Settings("scripted", None, None, workspace)
    runtime = DockerRuntime.start(settings, policy)
    try:
        # 镜像与宿主构建源的目录一致，真实 Worker 返回完整技能正文。
        assert runtime.skill_catalog == skill_loader().catalog()
        assert runtime.skill_catalog, "rebuild the image with trusted skills"
        name = next(iter(runtime.skill_catalog))
        body = runtime.skill_catalog[name]["body"]
        calls = (ToolCall("skill", "skill_loader", {"skill_name": name}),
                 ToolCall("read", "read_file", {"path": "source.txt"}))
        model = ScriptedLLM([LLMReply("", Message("assistant", "", calls), calls),
                             LLMReply("done", Message("assistant", "done"))])
        agent = Agent(model, default_tools(skill_catalog=runtime.skill_catalog),
                      DefaultPermissionPolicy(), settings, runtime)
        result = agent.run("load a skill and read source.txt")
        assert result.stop_reason == "completed"
        assert all(not item.is_error for item in result.tool_results)
        assert agent.active_skills[name] == body
        assert sum(message.content.count(body) for message in model.requests[1]) == 1
        agent.context.validate_tool_message_groups(model.requests[1])
        assert runtime.execute_tool("bash", {"command":
                                             "touch /opt/study-coder/skills/write-test"}).is_error
        identity = runtime.execute_tool("bash", {"command": "id -u"})
        assert not identity.is_error and "10001" in identity.content
        assert runtime.execute_tool("bash", {"command": "touch /root-write-test"}).is_error
        assert not runtime.execute_tool(
            "bash", {"command": "test ! -e /host && test ! -e /mnt/c"}
        ).is_error
        assert runtime.execute_tool(
            "bash",
            {"command": "python -c \"import socket; "
                        "socket.create_connection(('1.1.1.1', 53), 1)\""},
        ).is_error

        inspected = subprocess.run(
            [docker, "inspect", runtime.container], capture_output=True,
            text=True, timeout=15, check=True,
        )
        configuration = json.loads(inspected.stdout)[0]
        host = configuration["HostConfig"]
        assert configuration["Config"]["User"] == "10001:10001"
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert host["Memory"] == policy.memory_mb * 1024 * 1024
        assert host["NanoCpus"] == int(policy.cpus * 1_000_000_000)
        assert host["PidsLimit"] == policy.pids_limit

        timed_out = runtime.execute_tool(
            "bash", {"command": "(sleep 2; echo late > late.txt) & sleep 30", "timeout_s": 1},
        )
        assert timed_out.is_error and "timed out" in timed_out.content
        time.sleep(3)
        assert runtime.execute_tool("read_file", {"path": "late.txt"}).is_error

        assert not runtime.execute_tool("write_file", {"path": "output.txt",
                                                       "content": "before\n"}).is_error
        assert not runtime.execute_tool("bash", {"command":
                                                 "printf 'a\\nb\\n' | grep b > output.txt"}).is_error
        read = runtime.execute_tool("read_file", {"path": "output.txt"})
        assert "b" in read.content
        assert runtime.execute_tool("bash", {"command":
                                             "env | grep '^FAKE_SECRET='"}).is_error
        assert runtime.execute_tool("read_file", {"path": ".env"}).is_error
        assert not runtime.execute_tool("read_file", {"path": ".env.example"}).is_error
        assert not runtime.execute_tool("write_file", {"path": ".env.example",
                                                       "content": "FAKE_SECRET=placeholder\n",
                                                       "overwrite": True}).is_error
        diff = runtime.export_changes()
        assert "output.txt" in diff["added"]
        assert ".env.example" in diff["modified"]
        assert ".env" not in {name for paths in diff.values() for name in paths}
        assert not (workspace / "output.txt").exists()
        apply_task(policy, runtime.record["task_id"])
        assert (workspace / "output.txt").read_text(encoding="utf-8") == "b\n"
        assert (workspace / ".env").read_text(encoding="utf-8") == "FAKE_SECRET=host-canary\n"
        assert (workspace / ".env.example").read_text(encoding="utf-8") == "FAKE_SECRET=placeholder\n"
    finally:
        if runtime.running:
            runtime.cancel()
        if runtime.volume.startswith("study-coder-"):
            subprocess.run([docker, "volume", "rm", runtime.volume],
                           capture_output=True, timeout=15, check=False)
