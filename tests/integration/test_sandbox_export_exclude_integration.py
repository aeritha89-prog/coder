"""Docker 集成验收：用真实 GNU tar 验证导出排除规则及宿主文件应用。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from study_coder import cli
from study_coder.config import Settings
from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.docker import DockerRuntime, apply_task, cleanup_task, doctor
from study_coder.sandbox.manifest import load_record
from study_coder.sandbox.policy import SandboxPolicy


def test_real_export_cli_excludes_links_and_preserves_host_files(
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
    license_path = "toolchain/jdk25/legal/java.management/LICENSE"
    for relative, data in ((license_path, b"host license"), ("main.py", b"before"),
                           ("a/b", b"host root file"), ("nested/a/b", b"before")):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    settings = Settings("test", None, None, workspace)
    runtime = DockerRuntime.start(settings, policy)
    task_id = runtime.record["task_id"]
    try:
        script = """
mkdir -p /workspace/toolchain/jdk25/legal/java.base /workspace/gradlehome/caches /workspace/build/libs
printf 'container license' > /workspace/toolchain/jdk25/legal/java.base/LICENSE
rm /workspace/toolchain/jdk25/legal/java.management/LICENSE
ln -s ../java.base/LICENSE /workspace/toolchain/jdk25/legal/java.management/LICENSE
ln -s /etc/os-release /workspace/gradlehome/caches/link
printf after > /workspace/main.py
printf jar > /workspace/build/libs/app.jar
printf ignored > /workspace/a/b
printf after > /workspace/nested/a/b
"""
        subprocess.run([docker, "exec", runtime.container, "/bin/sh", "-ec", script],
                       capture_output=True, check=True, timeout=30)
        monkeypatch.setattr(cli, "load_policy", lambda: policy)
        monkeypatch.setattr(cli, "load_settings", lambda: settings)
        assert cli.main(["sandbox", "export", task_id, "--exclude", "toolchain/jdk25",
                         "--exclude", "gradlehome/caches", "--exclude", "a?b"]) == 0
        record = load_record(policy.state_dir, task_id)
        assert record["status"] == "awaiting_apply"
        assert record["diff"] == {"added": ["build/libs/app.jar"],
                                  "modified": ["main.py", "nested/a/b"], "deleted": []}
        apply_task(policy, task_id)
        assert (workspace / license_path).read_bytes() == b"host license"
        assert (workspace / "a/b").read_bytes() == b"host root file"
        assert (workspace / "nested/a/b").read_bytes() == b"after"
        assert (workspace / "main.py").read_bytes() == b"after"
        assert (workspace / "build/libs/app.jar").read_bytes() == b"jar"
        assert not (workspace / "gradlehome").exists()
    finally:
        # 仅清理此测试创建的任务资源；CLI 导出成功后通常已回收容器和 volume。
        cleanup_task(policy, task_id, discard=True)
