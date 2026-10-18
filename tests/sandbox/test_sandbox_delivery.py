"""交付前检验与开发 HOME 隔离；全部使用离线文件或 Docker 控制替身。"""

import pytest

from study_coder.config import Settings
from study_coder.sandbox import transfer
from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.docker import DockerRuntime, cleanup_task
from study_coder.sandbox.policy import SandboxPolicy


def test_export_preflight_names_large_files_and_respects_exclusions(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_FILE_BYTES", 16)
    (tmp_path / "main.py").write_bytes(b"pass")
    (tmp_path / "large.bin").write_bytes(b"x" * 17)
    problems = transfer.export_issues(tmp_path)
    assert len(problems) == 1 and "large.bin" in problems[0] and "16 bytes" in problems[0]
    assert transfer.export_issues(tmp_path, ("large.bin",)) == []


def test_preflight_rejects_links_but_accepts_regular_hardlinked_files(tmp_path):
    import os

    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")
    os.link(source, tmp_path / "hardlink.txt")
    assert transfer.export_issues(tmp_path) == []
    try:
        (tmp_path / "symbolic.txt").symlink_to(source)
    except OSError:
        pytest.skip("symbolic links require host support")
    assert any("symbolic.txt" in issue for issue in transfer.export_issues(tmp_path))


def test_preflight_is_bounded_and_uses_task_exclusions(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_FILE_BYTES", 1)
    for index in range(25):
        (tmp_path / f"artifact-{index}.bin").write_bytes(b"xx")
    assert len(transfer.export_issues(tmp_path)) == 21
    monkeypatch.setenv("STUDY_CODER_EXPORT_EXCLUDE", '["artifact-*.bin"]')
    assert transfer.runtime_export_issues(tmp_path, 1, 1) == []


def test_preflight_checks_total_bytes_and_member_count(tmp_path):
    (tmp_path / "a").write_bytes(b"xx")
    (tmp_path / "b").write_bytes(b"xx")
    assert any("workspace limit" in issue for issue in transfer.export_issues(tmp_path, max_total_bytes=3))
    assert any("count limit" in issue for issue in transfer.export_issues(tmp_path, max_files=1))


@pytest.mark.parametrize("fail_start", [False, True])
def test_task_home_volume_is_separate_and_cleaned(tmp_path, monkeypatch, fail_start):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = SandboxPolicy(state_dir=tmp_path / "state")
    commands = []
    removed = []
    monkeypatch.setattr("study_coder.sandbox.docker.doctor", lambda _: {"image_id": "sha256:test"})
    monkeypatch.setattr("study_coder.sandbox.docker._image_skill_catalog", lambda _: {})
    monkeypatch.setattr("study_coder.sandbox.docker._remove_owned_resource", lambda *args: removed.append(args))

    def control(args, **kwargs):
        commands.append(args)
        if fail_start and args[:2] == ["run", "-d"]:
            raise SandboxError("synthetic task start failure")
        return ""

    monkeypatch.setattr("study_coder.sandbox.docker._control", control)
    settings = Settings("test", None, None, workspace)
    if fail_start:
        with pytest.raises(SandboxError, match="synthetic"):
            DockerRuntime.start(settings, policy)
        assert any(kind == "volume" and name.startswith("study-coder-home-") for kind, name, _ in removed)
        return
    runtime = DockerRuntime.start(settings, policy)
    command = next(args for args in commands if args[:2] == ["run", "-d"])
    assert "HOME=/home/agent" in command and "HOME=/workspace" not in command
    assert f"type=volume,src={runtime.home_volume},dst=/home/agent" in command
    assert runtime.home_volume != runtime.volume
    assert "CARGO_HOME=/home/agent/.cargo" in command
    assert "GRADLE_USER_HOME=/home/agent/.gradle" in command
    assert not (workspace / ".cargo").exists()
    cleanup_task(policy, runtime.record["task_id"], discard=True)
    assert ("volume", runtime.home_volume, runtime.record["task_id"]) in removed
