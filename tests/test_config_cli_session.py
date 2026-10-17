from __future__ import annotations

from pathlib import Path

import pytest

import study_coder.cli as cli
from study_coder.cli import main
from study_coder.config import Settings, load_settings
from study_coder.llm import ScriptedLLM
from study_coder.session import load_session, save_session, session_path
from study_coder.types import LLMReply, Message, ToolCall, ToolResult


class StubDockerRuntime:
    """CLI 单元测试中的容器替身；不执行工作区命令。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.record = {"task_id": "test-task"}
        self.skill_catalog: dict[str, dict[str, str]] = {}
        self.incomplete_tasks: dict[str, str] = {}

    def execute_tool(self, name: str, arguments: dict) -> ToolResult:
        return ToolResult("", name, "simulated container result", name == "read_file")

    def export_changes(self) -> dict[str, list[str]]:
        return {"added": [], "modified": [], "deleted": []}

    def get_incomplete_tasks(self) -> dict[str, str]:
        return dict(self.incomplete_tasks)

    def get_task_state(self) -> str:
        return ""

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def docker_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StubDockerRuntime:
    runtime = StubDockerRuntime(tmp_path / "runtime")
    monkeypatch.setenv("STUDY_CODER_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli.DockerRuntime, "start", lambda settings, policy: runtime)
    return runtime


@pytest.mark.parametrize("stream", [False, True])
def test_cli_incomplete_reply_is_shown_once(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, docker_stub: StubDockerRuntime, stream: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    docker_stub.incomplete_tasks = {"task_a1b2c3d4": "pending"}
    text = "Please provide the model service address."

    def fake_model(settings: Settings, **kwargs: object) -> ScriptedLLM:
        class StreamingModel(ScriptedLLM):
            def complete(self, messages: list[Message], tools: list[dict]) -> LLMReply:
                reply = super().complete(messages, tools)
                callback = kwargs.get("on_text")
                if kwargs.get("stream") and callable(callback):
                    callback(reply.text)
                return reply
        return StreamingModel([LLMReply(text, Message("assistant", text))])

    monkeypatch.setattr(cli, "OpenAICompatibleModel", fake_model)
    inputs = iter(["build an app", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    args = ["-C", str(workspace), "--max-rounds", "1"]
    if stream:
        args.append("--stream")
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.out.count(text) == 1
    assert "计划仍有未完成事项，本轮已结束" in captured.err


def test_config_and_cli(workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
                        monkeypatch: pytest.MonkeyPatch,
                        docker_stub: StubDockerRuntime) -> None:
    # CLI 从当前目录读取 .env；这个测试需要隔离开发者真实的本地配置。
    monkeypatch.chdir(tmp_path)
    settings = load_settings({"STUDY_CODER_WORKSPACE": str(workspace)})
    assert settings.api_key is None
    assert (settings.model, settings.base_url) == ("qwen3.5:4b", "http://127.0.0.1:11434/v1")
    with pytest.raises(ValueError):
        Settings("x", None, None, workspace, max_rounds=0)
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert main(["-C", str(workspace / "missing")]) == 2
    assert main(["-C", str(workspace), "--max-rounds", "0"]) == 2
    monkeypatch.delenv("STUDY_CODER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("STUDY_CODER_BASE_URL", "https://example.test/v1")
    assert main(["-C", str(workspace)]) == 2
    assert "API key required" in capsys.readouterr().err


def test_local_backend_is_rejected(workspace: Path) -> None:
    with pytest.raises(ValueError, match="only Docker execution is supported"):
        load_settings({"STUDY_CODER_WORKSPACE": str(workspace),
                       "STUDY_CODER_EXECUTION_BACKEND": "local"})
    with pytest.raises(SystemExit) as failure:
        main(["--sandbox", "local"])
    assert failure.value.code == 2


def test_session_name_and_roundtrip(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace)
    with pytest.raises(ValueError):
        session_path(workspace, "../escape")
    original = [Message("system", "policy"), Message("user", "hello")]
    save_session(workspace, "run_1", settings, original)
    assert load_session(workspace, "run_1", settings) == original


def test_dotenv_settings_and_shell_priority(workspace: Path, tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "STUDY_CODER_API_KEY=file-key\nSTUDY_CODER_MODEL=file-model\n"
        "STUDY_CODER_BASE_URL=https://example.test/v1\nSTUDY_CODER_MAX_ROUNDS=4\n"
        "STUDY_CODER_REQUEST_TIMEOUT_S=30\n", encoding="utf-8")
    from_file = load_settings({"STUDY_CODER_WORKSPACE": str(workspace)}, dotenv_path)
    assert (from_file.api_key, from_file.model, from_file.max_rounds) == ("file-key", "file-model", 4)
    assert from_file.base_url == "https://example.test/v1"
    assert from_file.request_timeout_s == 30
    overridden = load_settings({"STUDY_CODER_WORKSPACE": str(workspace),
                                "OPENAI_API_KEY": "shell-key", "STUDY_CODER_MODEL": "shell-model"},
                               dotenv_path)
    assert (overridden.api_key, overridden.model) == ("shell-key", "shell-model")


def test_cli_reads_dotenv_and_cli_wins(workspace: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch,
                                       docker_stub: StubDockerRuntime) -> None:
    (tmp_path / ".env").write_text("STUDY_CODER_API_KEY=file-key\n"
                                   "STUDY_CODER_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for key in ("STUDY_CODER_API_KEY", "OPENAI_API_KEY", "STUDY_CODER_MODEL"):
        monkeypatch.delenv(key, raising=False)
    captured: list[Settings] = []

    def fake_model(settings: Settings, **kwargs: object) -> ScriptedLLM:
        captured.append(settings)
        return ScriptedLLM([LLMReply("done", Message("assistant", "done"))])

    monkeypatch.setattr(cli, "OpenAICompatibleModel", fake_model)
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    assert main(["-C", str(workspace), "--model", "cli-model"]) == 0
    assert captured[0].api_key == "file-key"
    assert captured[0].model == "cli-model"


@pytest.mark.parametrize("stream", [False, True])
def test_cli_prints_tool_counts_after_each_task(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, stream: bool,
    docker_stub: StubDockerRuntime,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = (
        ToolCall("call-1", "glob", {"pattern": "*.py"}),
        ToolCall("call-2", "read_file", {"path": "missing.txt"}),
    )

    def fake_model(settings: Settings, **kwargs: object) -> ScriptedLLM:
        return ScriptedLLM([
            LLMReply("", Message("assistant", "", calls), calls),
            LLMReply("done", Message("assistant", "done")),
        ])

    monkeypatch.setattr(cli, "OpenAICompatibleModel", fake_model)
    inputs = iter(["inspect", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    args = ["-C", str(workspace)]
    if stream:
        args.append("--stream")
    assert main(args) == 0
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert lines[-3:] == [
        "[工具统计] 总调用次数=2",
        '[工具统计] "glob" 次数=1',
        '[工具统计] "read_file" 次数=1',
    ]
    assert "[工具统计]" not in captured.out
    assert "call-1" not in captured.err and "call-2" not in captured.err


@pytest.mark.parametrize("stream", [False, True])
def test_cli_reports_truncated_reply_as_incomplete(workspace, tmp_path, capsys, monkeypatch,
                                                  docker_stub, stream):
    monkeypatch.chdir(tmp_path)
    reply = LLMReply("partial answer", Message("assistant", "partial answer"), finish_reason="length")
    monkeypatch.setattr(cli, "OpenAICompatibleModel", lambda *args, **kwargs: ScriptedLLM([reply]))
    inputs = iter(["implement", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    args = ["-C", str(workspace)] + (["--stream"] if stream else [])
    assert main(args) == 1
    assert "模型答复因长度限制被截断" in capsys.readouterr().err


def test_cli_interactive_tool_counts_are_per_task(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    docker_stub: StubDockerRuntime,
) -> None:
    monkeypatch.chdir(tmp_path)
    first_calls = (
        ToolCall("a", "glob", {"pattern": "*.py"}),
        ToolCall("b", "glob", {"pattern": "*.txt"}),
    )

    def fake_model(settings: Settings, **kwargs: object) -> ScriptedLLM:
        return ScriptedLLM([
            LLMReply("", Message("assistant", "", first_calls), first_calls),
            LLMReply("first done", Message("assistant", "first done")),
            LLMReply("second done", Message("assistant", "second done")),
        ])

    inputs = iter(["first task", "second task", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr(cli, "OpenAICompatibleModel", fake_model)
    assert main(["-C", str(workspace)]) == 0
    stats = [line for line in capsys.readouterr().err.splitlines()
             if line.startswith("[工具统计]")]
    assert stats == [
        "[工具统计] 总调用次数=2",
        '[工具统计] "glob" 次数=2',
        "[工具统计] 总调用次数=0",
    ]


@pytest.mark.parametrize("option", ["-p", "--prompt"])
def test_cli_rejects_removed_prompt_option(option: str) -> None:
    with pytest.raises(SystemExit) as failure:
        cli.parser().parse_args([option, "hello"])
    assert failure.value.code == 2


def test_dialogue_requests_permission_and_exports_only_after_exit(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    docker_stub: StubDockerRuntime,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = (ToolCall("command", "bash", {"command": "echo simulated"}),)
    model = ScriptedLLM([
        LLMReply("", Message("assistant", "", calls), calls),
        LLMReply("first complete", Message("assistant", "first complete")),
        LLMReply("second complete", Message("assistant", "second complete")),
    ])
    monkeypatch.setattr(cli, "OpenAICompatibleModel", lambda *args, **kwargs: model)
    exports: list[str] = []
    executed: list[str] = []

    def export() -> dict[str, list[str]]:
        exports.append("export")
        return {"added": [], "modified": [], "deleted": []}

    def execute(name: str, arguments: dict) -> ToolResult:
        executed.append(name)
        return ToolResult("", name, "simulated")

    monkeypatch.setattr(docker_stub, "export_changes", export)
    monkeypatch.setattr(docker_stub, "execute_tool", execute)
    inputs = iter(["first task", "y", "second task", "exit"])
    prompts: list[str] = []

    def read_input(prompt: str) -> str:
        # 到下一次用户输入（包括授权）时，同一个沙箱仍未被结束导出。
        assert exports == []
        prompts.append(prompt)
        return next(inputs)

    monkeypatch.setattr("builtins.input", read_input)
    assert main(["-C", str(workspace)]) == 0
    assert prompts == ["study-coder> ", "> ", "study-coder> ", "study-coder> "]
    assert executed == ["bash"] and exports == ["export"]
    assert [m.content for m in model.requests[-1] if m.role == "user"][-2:] == [
        "first task", "second task"]


def test_dialogue_eof_exports_once_without_model_request(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    docker_stub: StubDockerRuntime,
) -> None:
    monkeypatch.chdir(tmp_path)
    model = ScriptedLLM([])
    monkeypatch.setattr(cli, "OpenAICompatibleModel", lambda *args, **kwargs: model)
    exports: list[str] = []

    def export() -> dict[str, list[str]]:
        exports.append("export")
        return {"added": [], "modified": [], "deleted": []}

    def end_input(prompt: str) -> str:
        raise EOFError()

    monkeypatch.setattr(docker_stub, "export_changes", export)
    monkeypatch.setattr("builtins.input", end_input)
    assert main(["-C", str(workspace)]) == 0
    assert model.requests == [] and exports == ["export"]
