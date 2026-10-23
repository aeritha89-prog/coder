"""Harbor 外部 Agent：模型循环留在宿主，工具调用转发进任务容器。

Harbor 创建并持有容器；本类只负责把 zipapp 送进去、对齐 ``/workspace``，
然后用 ``HarborRuntime`` 驱动现有的 ``coder.agent.Agent``。
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import shlex
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from coder.agent import Agent
from coder.config import Settings, load_settings
from coder.llm import OpenAICompatibleModel
from coder.permissions import DefaultPermissionPolicy
from coder.sandbox.policy import SandboxPolicy
from coder.tools import default_tools
from evals.tb.build_worker import build_worker_zip
from evals.tb.result import apply_outcome, write_trajectory
from evals.tb.runtime import WORKER_DIR, WORKER_PATH, HarborRuntime

# Worker 把工作区写死为 /workspace；TB 任务通常在 /app，靠符号链接对齐。
CONTAINER_WORKSPACE = "/workspace"
_MIN_PYTHON = (3, 10)
_SETUP_TIMEOUT_S = 120
_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_UV_BINARY = (_PROJECT_ROOT / ".cache" / "evals" / "uv-linux"
              / "uv-x86_64-unknown-linux-gnu" / "uv")
# 这些任务的 verifier 会在容器里现装 uv/apt 包；setup 阶段准备相应依赖。
_UV_IMAGES = ("merge-diff-arc-agi-task", "qemu-startup", "write-compressor")
_APT_FIX_SCRIPT = r"""
if [ -f /etc/os-release ] && grep -q 'VERSION_CODENAME=bullseye' /etc/os-release; then
  if ! grep -q 'archive.debian.org' /etc/apt/sources.list 2>/dev/null; then
    sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g; s|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' /etc/apt/sources.list 2>/dev/null || true
    echo 'deb http://archive.debian.org/debian bullseye main' >> /etc/apt/sources.list
    echo 'deb http://archive.debian.org/debian-security bullseye-security main' >> /etc/apt/sources.list
  fi
  apt-get -o Acquire::Check-Valid-Until=false update -qq >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl expect >/dev/null 2>&1 || true
fi
command -v curl || true
command -v expect || true
"""
_DETECT_PYTHON = (
    "for candidate in python3 python; do "
    "if command -v $candidate >/dev/null 2>&1; then "
    "echo CANDIDATE:$candidate; "
    "$candidate -c 'import sys; print(\"VERSION:%d.%d\" % sys.version_info[:2])' "
    "2>/dev/null || true; "
    "fi; done"
)


class CoderAgent(BaseAgent):
    """Harbor external agent：容器所有权归 Harbor，导出与评分也由它负责。"""

    @staticmethod
    def name() -> str:
        return "coder"

    def version(self) -> str | None:
        try:
            return importlib.metadata.version("coder")
        except importlib.metadata.PackageNotFoundError:
            return None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._python: str | None = None
        self._python_version: str | None = None
        self._workdir: str | None = None
        self._workspace_linked = False
        self._setup_notes: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    async def setup(self, environment: BaseEnvironment) -> None:
        """上传 zipapp、探测解释器，并把 /workspace 指向任务工作目录。"""
        archive = build_worker_zip()
        mkdir = await environment.exec(f"mkdir -p {shlex.quote(WORKER_DIR)}",
                                       timeout_sec=_SETUP_TIMEOUT_S)
        if mkdir.return_code != 0:
            raise RuntimeError("cannot create worker directory in task container: "
                               f"{mkdir.stderr or mkdir.stdout or 'no output'}")
        await environment.upload_file(archive, WORKER_PATH)

        self._python, self._python_version = await self._detect_python(environment)
        self._workdir = await self._detect_workdir(environment)
        self._workspace_linked = await self._link_workspace(environment, self._workdir)

        if self._python is not None:
            probe = await environment.exec(f"{shlex.quote(self._python)} {WORKER_PATH} "
                                           "< /dev/null", timeout_sec=_SETUP_TIMEOUT_S)
            # 空请求应得到一条 JSON 错误；这同时验证归档能导入全部依赖。
            if probe.return_code != 0 or '"name": "sandbox"' not in (probe.stdout or ""):
                raise RuntimeError(
                    "container worker failed to start: "
                    f"{probe.stderr or probe.stdout or 'no output'}")
        if not self._workspace_linked and self._python is not None:
            raise RuntimeError(
                f"cannot link {CONTAINER_WORKSPACE} to {self._workdir!r}; "
                "the worker requires that path")

        await self._prepare_verifier_tools(environment)

        self._setup_notes = {"python": self._python_version, "workdir": self._workdir,
                             "workspace_linked": self._workspace_linked}
        self.logger.info("coder setup: python=%s workdir=%s linked=%s",
                         self._python_version, self._workdir, self._workspace_linked)

    @staticmethod
    async def _detect_python(environment: BaseEnvironment) -> tuple[str | None, str | None]:
        """优先 python3；任务镜像缺少可用解释器时退化为 bash-only。"""
        result = await environment.exec(_DETECT_PYTHON, timeout_sec=_SETUP_TIMEOUT_S)
        candidate: str | None = None
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("CANDIDATE:"):
                candidate = line.removeprefix("CANDIDATE:")
            elif line.startswith("VERSION:") and candidate is not None:
                raw = line.removeprefix("VERSION:")
                try:
                    major, minor = (int(part) for part in raw.split(".", 1))
                except ValueError:
                    continue
                if (major, minor) >= _MIN_PYTHON:
                    return candidate, raw
        return None, None

    @staticmethod
    async def _detect_workdir(environment: BaseEnvironment) -> str:
        """任务配置优先；否则读取容器当前 WORKDIR。"""
        configured = environment.task_env_config.workdir
        if configured:
            result = await environment.exec(
                f"cd {shlex.quote(configured)} && pwd", timeout_sec=_SETUP_TIMEOUT_S)
            if result.return_code == 0 and (result.stdout or "").strip():
                return (result.stdout or "").strip().splitlines()[-1]
        result = await environment.exec("pwd", timeout_sec=_SETUP_TIMEOUT_S)
        return (result.stdout or "").strip().splitlines()[-1] if result.stdout else "/"

    async def _link_workspace(self, environment: BaseEnvironment, workdir: str) -> bool:
        """让 /workspace 成为任务目录的链接；已有非空目录时保持原样。"""
        quoted = shlex.quote(workdir)
        script = (
            f"target={quoted}; "
            f'if [ "$target" = "{CONTAINER_WORKSPACE}" ]; then echo ok:same; exit 0; fi; '
            f'if [ -L {CONTAINER_WORKSPACE} ]; then '
            f"ln -sfn \"$target\" {CONTAINER_WORKSPACE} && echo ok:relinked; exit 0; fi; "
            f'if [ -d {CONTAINER_WORKSPACE} ]; then '
            f"rmdir {CONTAINER_WORKSPACE} 2>/dev/null && "
            f"ln -s \"$target\" {CONTAINER_WORKSPACE} && echo ok:replaced; exit 0; fi; "
            f"if [ -e {CONTAINER_WORKSPACE} ]; then echo blocked:file; exit 0; fi; "
            f"ln -s \"$target\" {CONTAINER_WORKSPACE} && echo ok:created || echo blocked:denied")
        result = await environment.exec(script, timeout_sec=_SETUP_TIMEOUT_S)
        output = (result.stdout or "").strip()
        linked = result.return_code == 0 and output.startswith("ok:")
        if not linked:
            self.logger.warning("cannot prepare %s: %s", CONTAINER_WORKSPACE,
                                output or result.stderr or "no output")
        return linked

    async def _prepare_verifier_tools(self, environment: BaseEnvironment) -> None:
        """给少数镜像定向补齐 verifier 依赖；失败只告警，不阻断 agent。"""
        image = getattr(environment.task_env_config, "docker_image", "") or ""
        if any(name in image for name in _UV_IMAGES):
            if not _UV_BINARY.is_file():
                self.logger.warning("uv binary is missing: %s", _UV_BINARY)
            else:
                try:
                    await environment.upload_file(_UV_BINARY, "/usr/local/bin/uv")
                    result = await environment.exec(
                        "chmod +x /usr/local/bin/uv && "
                        "ln -sf /usr/local/bin/uv /usr/local/bin/uvx && "
                        "/usr/local/bin/uvx --version",
                        timeout_sec=_SETUP_TIMEOUT_S)
                    if result.return_code != 0:
                        self.logger.warning("cannot prepare uvx: %s",
                                            result.stderr or result.stdout or "no output")
                except (OSError, RuntimeError) as exc:
                    self.logger.warning("cannot upload uv binary: %s", exc)
        if "qemu-startup" in image:
            result = await environment.exec(_APT_FIX_SCRIPT, timeout_sec=300)
            if result.return_code != 0:
                self.logger.warning("cannot prepare apt packages: %s",
                                    result.stderr or result.stdout or "no output")

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        """在宿主线程跑 Agent 循环；每次工具调用回到 Harbor 的事件循环。"""
        loop = asyncio.get_running_loop()
        host_workspace = Path(tempfile.mkdtemp(prefix="coder-tb-"))
        try:
            settings = self._settings(host_workspace)
            policy = SandboxPolicy(background_enabled=False,
                                   workspace_max_bytes=64 * 1024**3,
                                   workspace_max_files=1_000_000)
            runtime = HarborRuntime(environment, loop, settings, python=self._python,
                                    workspace_path=self._runtime_workspace(),
                                    policy=policy)
            agent = Agent(model=OpenAICompatibleModel(settings),
                          tools=default_tools(memory_enabled=False,
                                              background_enabled=False,
                                              service_ports=(),
                                              skill_catalog={}),
                          permission=DefaultPermissionPolicy(yes=True),
                          settings=settings, runtime=runtime)
            try:
                outcome = await asyncio.to_thread(agent.run, instruction)
            except asyncio.CancelledError:
                # Harbor 超时后取消协程；线程无法强杀，只阻止后续工具调用。
                runtime.cancel()
                raise
            apply_outcome(context, outcome, settings.model, self._setup_notes)
            write_trajectory(self.logs_dir, outcome, settings.model)
            if outcome.stop_reason == "failed":
                raise RuntimeError(f"coder run failed: {outcome.text}")
        finally:
            shutil.rmtree(host_workspace, ignore_errors=True)

    def _runtime_workspace(self) -> str:
        """bash-only 降级时直接使用真实工作目录。"""
        if self._workspace_linked or self._workdir is None:
            return CONTAINER_WORKSPACE
        return self._workdir

    def _settings(self, workspace: Path) -> Settings:
        """模型凭据来自 Harbor 进程环境；预算不低于 TB 所需的默认值。"""
        dotenv = Path.cwd() / ".env"
        base = load_settings(env={**os.environ, **self._extra_env},
                             dotenv_path=dotenv if dotenv.is_file() else None)
        return replace(
            base,
            model=self.model_name or base.model,
            workspace=workspace,
            max_rounds=max(base.max_rounds, 2000),
            command_timeout_s=max(base.command_timeout_s, 1800),
            context_window_tokens=max(base.context_window_tokens, 200_000),
            memory_enabled=False,
        )
