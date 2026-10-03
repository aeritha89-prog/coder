"""读取 .env 与环境变量，并在创建设置时校验工作目录和运行预算。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    """一次 Agent 运行共享的配置；冻结后各组件看到相同的工作区边界。"""

    model: str
    api_key: str | None
    base_url: str | None
    workspace: Path
    max_rounds: int = 2000
    command_timeout_s: int = 60
    max_tool_output_chars: int = 12_000
    context_window_tokens: int = 32_000
    request_timeout_s: int = 60
    max_skill_chars: int = 40_000
    max_active_skill_chars: int = 60_000

    def __post_init__(self) -> None:
        """将工作区固定为真实路径，避免相对路径随进程目录变化。"""
        root = self.workspace.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be an existing directory")
        object.__setattr__(self, "workspace", root)
        for name in ("max_rounds", "command_timeout_s", "max_tool_output_chars",
                     "context_window_tokens", "request_timeout_s", "max_skill_chars",
                     "max_active_skill_chars"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_skill_chars", "max_active_skill_chars"):
            if type(getattr(self, name)) is not int or getattr(self, name) > 1_000_000:
                raise ValueError(f"{name} must be an integer from 1 to 1000000")
        if not self.model.strip():
            raise ValueError("model must not be empty")


def load_settings(env: Mapping[str, str] | None = None,
                  dotenv_path: Path | None = None) -> Settings:
    """读取启动目录的 .env 默认值，再用进程环境覆盖。

    测试可传入 env 映射隔离真实环境；只有显式指定 dotenv_path 时才读取文件。
    API key 兼容项目专用名称和 OPENAI_API_KEY，且环境变量优先于文件。
    """
    shell_values = os.environ if env is None else env
    # 显式 env 映射通常用于测试，不应意外读取调用者当前目录下的 .env。
    file_values = (dotenv_values(dotenv_path or Path.cwd() / ".env")
                   if env is None or dotenv_path is not None else {})
    values = {key: value for key, value in file_values.items() if value is not None}
    values.update(shell_values)
    # 旧配置只能显式选择 Docker；拒绝 local，避免静默切换执行环境。
    if values.get("STUDY_CODER_EXECUTION_BACKEND") not in (None, "", "docker"):
        raise ValueError("only Docker execution is supported")
    api_key = (shell_values.get("STUDY_CODER_API_KEY") or shell_values.get("OPENAI_API_KEY")
               or file_values.get("STUDY_CODER_API_KEY") or file_values.get("OPENAI_API_KEY"))
    return Settings(
        model=values.get("STUDY_CODER_MODEL", "qwen3.5:4b"),
        api_key=api_key or None,
        base_url=values.get("STUDY_CODER_BASE_URL") or "http://127.0.0.1:11434/v1",
        workspace=Path(values.get("STUDY_CODER_WORKSPACE", ".")),
        max_rounds=int(values.get("STUDY_CODER_MAX_ROUNDS", "200")),
        command_timeout_s=int(values.get("STUDY_CODER_COMMAND_TIMEOUT_S", "60")),
        max_tool_output_chars=int(values.get("STUDY_CODER_MAX_TOOL_OUTPUT_CHARS", "12800")),
        context_window_tokens=int(values.get("STUDY_CODER_CONTEXT_WINDOW_TOKENS", "200000")),
        request_timeout_s=int(values.get("STUDY_CODER_REQUEST_TIMEOUT_S", "60")),
        max_skill_chars=int(values.get("STUDY_CODER_MAX_SKILL_CHARS", "40000")),
        max_active_skill_chars=int(values.get("STUDY_CODER_MAX_ACTIVE_SKILL_CHARS", "60000")),
    )
