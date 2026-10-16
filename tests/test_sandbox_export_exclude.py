"""旧任务追加导出排除规则，保护原文件，并在失败后继续重试。"""

from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from study_coder import cli
from study_coder.config import Settings
from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.docker import DockerRuntime, apply_task, policy_for_task
from study_coder.sandbox.manifest import load_record, save_record
from study_coder.sandbox.policy import SandboxPolicy
from study_coder.sandbox.transfer import filter_snapshot, tar_exclude_patterns

TASK_ID = "1" * 32
SDK = "toolchain/jdk25"
LICENSE = SDK + "/legal/java.management/LICENSE"


def _tar(entries: list[tuple[str, bytes | None]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in entries:
            member = tarfile.TarInfo("./" + name)
            if data is None:
                member.type = tarfile.SYMTYPE
                member.linkname = "../java.base/LICENSE"
            else:
                member.size = len(data)
            archive.addfile(member, io.BytesIO(data) if data is not None else None)
    return stream.getvalue()


@pytest.fixture
def exporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
             ) -> tuple[DockerRuntime, list[list[str]], list[bytes]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = {}
    for relative, data in (("main.py", b"before"), ("obsolete.txt", b"old"),
                           (LICENSE, b"original license")):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        snapshot[relative] = hashlib.sha256(data).hexdigest()
    policy = SandboxPolicy(state_dir=tmp_path / "state", exclude=("startup-cache",))
    record = {
        "task_id": TASK_ID, "workspace": str(workspace), "status": "export_failed",
        "image": policy.image, "image_id": "sha256:test", "container": "test-container",
        "volume": "test-volume", "snapshot": snapshot, "exported": {}, "diff": {},
        "policy": {**asdict(policy), "state_dir": str(policy.state_dir)},
    }
    save_record(policy.state_dir / TASK_ID, record)
    runtime = DockerRuntime(Settings("test", None, None, workspace), policy, record)
    commands: list[list[str]] = []
    payloads: list[bytes] = []

    class Process:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            commands.append(command)
            self.stdout = io.BytesIO(payloads.pop(0))
            self.stderr = io.BytesIO()
            self.returncode = 0

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            self.returncode = 1

    monkeypatch.setattr("study_coder.sandbox.docker._docker_executable", lambda: "docker")
    monkeypatch.setattr("study_coder.sandbox.docker._control", lambda *a, **k: "")
    monkeypatch.setattr("study_coder.sandbox.docker._remove_owned_resource", lambda *a: None)
    monkeypatch.setattr(DockerRuntime, "_ensure_stopped", lambda self: None)
    monkeypatch.setattr("study_coder.sandbox.docker.subprocess.Popen", Process)
    return runtime, commands, payloads


def test_cli_forwards_repeated_excludes_and_restores_saved_policy(
    exporter: tuple[DockerRuntime, list[list[str]], list[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _ = exporter
    seen: dict[str, Any] = {}

    class Runtime:
        def __init__(self, settings: Settings, policy: SandboxPolicy,
                     record: dict[str, Any]) -> None:
            seen["policy"] = policy

        def export_changes(self, *, extra_exclude: tuple[str, ...]) -> dict[str, list[str]]:
            seen["exclude"] = extra_exclude
            return {"added": [], "modified": [], "deleted": []}

    monkeypatch.setattr(cli, "load_policy", lambda: replace(runtime.policy, exclude=("new-env",)))
    monkeypatch.setattr(cli, "load_settings", lambda: runtime.settings)
    monkeypatch.setattr(cli, "DockerRuntime", Runtime)
    assert cli.main(["sandbox", "export", TASK_ID,
                     "--exclude", SDK, "--exclude", "gradlehome/caches"]) == 0
    assert seen["policy"].exclude == ("startup-cache",)
    assert seen["exclude"] == (SDK, "gradlehome/caches")


@pytest.mark.parametrize("action", ["review", "apply"])
def test_cli_exclude_is_only_for_export(action: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["sandbox", action, TASK_ID, "--exclude", SDK])
    assert error.value.code == 2
    assert "--exclude is only supported for sandbox export" in capsys.readouterr().err


def test_export_exclusions_reach_tar_and_host_and_preserve_original_files(
    exporter: tuple[DockerRuntime, list[list[str]], list[bytes]],
) -> None:
    runtime, commands, payloads = exporter
    payloads.append(_tar([(LICENSE, None), ("gradlehome/caches/link", None),
                          ("main.py", b"after"), ("build/libs/app.jar", b"jar")]))
    before = dict(runtime.record["snapshot"])
    diff = runtime.export_changes(extra_exclude=("toolchain\\jdk25\\", "./gradlehome/caches/", SDK))
    assert {"--exclude=./startup-cache", "--exclude=startup-cache",
            "--exclude=./toolchain/jdk25",
            "--exclude=./gradlehome/caches", "--no-wildcards-match-slash"}.issubset(commands[0])
    saved = load_record(runtime.policy.state_dir, TASK_ID)
    assert saved["export_exclude"] == [SDK, "gradlehome/caches"]
    assert saved["policy"]["exclude"] == ["startup-cache"]
    assert saved["snapshot"] == before
    assert LICENSE not in saved["apply_snapshot"]
    assert diff == {"added": ["build/libs/app.jar"], "modified": ["main.py"],
                    "deleted": ["obsolete.txt"]}
    apply_task(runtime.policy, TASK_ID)
    assert (runtime.settings.workspace / LICENSE).read_bytes() == b"original license"
    assert (runtime.settings.workspace / "main.py").read_bytes() == b"after"
    assert (runtime.settings.workspace / "build/libs/app.jar").read_bytes() == b"jar"
    assert not (runtime.settings.workspace / "obsolete.txt").exists()
    finished = DockerRuntime(runtime.settings, runtime.policy,
                             load_record(runtime.policy.state_dir, TASK_ID))
    assert finished.export_changes(extra_exclude=(SDK,)) == diff
    with pytest.raises(SandboxError, match="already exported"):
        finished.export_changes(extra_exclude=("another-cache",))
    assert len(commands) == 1


def test_failed_export_persists_exclusions_for_retry_without_flags(
    exporter: tuple[DockerRuntime, list[list[str]], list[bytes]],
) -> None:
    runtime, commands, payloads = exporter
    payloads.append(_tar([("unexpected-link", None)]))
    with pytest.raises(SandboxError, match="unsupported export entry"):
        runtime.export_changes(extra_exclude=(SDK,))
    saved = load_record(runtime.policy.state_dir, TASK_ID)
    assert saved["status"] == "export_failed"
    assert saved["export_exclude"] == [SDK]
    payloads.append(_tar([(LICENSE, None), ("main.py", b"after")]))
    retried = DockerRuntime(runtime.settings, policy_for_task(runtime.policy, saved), saved)
    retried.export_changes()
    assert "--exclude=./toolchain/jdk25" in commands[1]
    assert load_record(runtime.policy.state_dir, TASK_ID)["status"] == "awaiting_apply"


@pytest.mark.parametrize("pattern", ["", ".", "../outside", "/absolute", "C:\\absolute"])
def test_invalid_export_exclusions_fail_before_changing_task(
    exporter: tuple[DockerRuntime, list[list[str]], list[bytes]], pattern: str,
) -> None:
    runtime, commands, _ = exporter
    with pytest.raises(SandboxError, match="workspace-relative patterns"):
        runtime.export_changes(extra_exclude=(pattern,))
    assert "export_exclude" not in load_record(runtime.policy.state_dir, TASK_ID)
    assert commands == []


def test_relative_directory_exclusion_preserves_nested_namesake() -> None:
    snapshot = {LICENSE: "sdk", "nested/" + LICENSE: "project", "main.py": "source"}
    assert filter_snapshot(snapshot, (SDK,)) == {
        "nested/" + LICENSE: "project", "main.py": "source"}
    patterns = tar_exclude_patterns((SDK,))
    assert "./toolchain/jdk25" in patterns
    assert "toolchain/jdk25" not in patterns


@pytest.mark.parametrize("pattern", ["?", "*.*"])
def test_custom_tar_globs_cannot_match_root_operand(pattern: str) -> None:
    patterns = tar_exclude_patterns((pattern,))
    assert pattern not in patterns
    assert "./" + pattern in patterns
    assert "./*/" + pattern in patterns
