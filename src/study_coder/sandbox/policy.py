"""宿主可信的 Sandbox 策略；模型传参不能改变这些上限。"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

from study_coder.sandbox.base import SandboxError


@dataclass(frozen=True)
class SandboxPolicy:
    """一次任务的宿主可信配置；创建后不可变，并随任务记录保存。

    与模型传给 bash 的 timeout_s 不同，这些值只来自用户配置/CLI。
    Worker 会再验证请求结构，但不能替代宿主这里的上限判断。
    """

    # 镜像与网络：默认完全离线；allowlist 时代理只转发批准域名。
    image: str = "study-coder-sandbox:general"
    profile: str = "general"
    network: str = "none"
    network_allowlist: tuple[str, ...] = ()
    network_max_bytes: int = 536_870_912
    trust_fake_ip_dns: bool = False
    # 容器级 CPU、内存、进程数，以及每次工具调用和工作区的逻辑预算。
    cpus: float = 2.0
    memory_mb: int = 4096
    pids_limit: int = 128
    max_command_timeout_s: int = 1800  # 宿主允许的单命令最大秒数，前台／后台共同受限。
    command_output_bytes: int = 1_048_576  # 每条命令保留的日志字节预算；不是模型结果字符数。
    # 开关控制模型工具注册和实际路由；并发数只来自宿主，模型不能提高它。
    background_enabled: bool = True
    # 已结束但未读取的结果不占运行名额；清理未确认项仍占。各任务共享容器资源，
    # 这个数不是独立容器数，也不意味着后台运行时冻结工作区编辑。
    max_background_jobs: int = 4
    workspace_max_bytes: int = 1_073_741_824
    workspace_max_files: int = 50_000
    # web_search 在宿主执行；预览端口和导出应用策略需事先由用户配置。
    allow_web_search: bool = False
    service_ports: tuple[int, ...] = ()
    apply: str = "review"
    state_dir: Path = Path.home() / ".study-coder" / "sandbox"
    exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # 先验证策略组合，再检查每个数值范围；失败时不要启动任务容器。
        if self.profile != "general":
            raise SandboxError("only the general sandbox profile is implemented")
        if self.network not in {"none", "allowlist"}:
            raise SandboxError("sandbox network must be none or allowlist")
        if self.network == "none" and self.network_allowlist:
            raise SandboxError("network allowlist requires network=allowlist")
        if self.trust_fake_ip_dns and self.network != "allowlist":
            raise SandboxError("fake-IP DNS support requires network=allowlist")
        if self.network == "allowlist":
            # 白名单只收域名或 *.子域名，不把 IP 地址当作可批准的目标。
            if not 1 <= len(self.network_allowlist) <= 100:
                raise SandboxError("network allowlist requires 1..100 domains")
            for entry in self.network_allowlist:
                domain = entry.removeprefix("*.").rstrip(".").casefold()
                labels = domain.split(".")
                if (len(domain) > 253 or len(labels) < 2
                        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                               for label in labels)):
                    raise SandboxError(f"invalid network allowlist domain: {entry}")
                try:
                    ipaddress.ip_address(domain)
                except ValueError:
                    pass
                else:
                    raise SandboxError("network allowlist entries must be domain names")
        if not 16 * 1024 * 1024 <= self.network_max_bytes <= 8 * 1024**3:
            raise SandboxError("sandbox network byte limit must be 16 MiB..8 GiB")
        if not self.image or any(ch.isspace() for ch in self.image) or self.image.startswith("-"):
            raise SandboxError("invalid sandbox image name")
        if not 0 < self.cpus <= 64 or not 128 <= self.memory_mb <= 262_144:
            raise SandboxError("sandbox CPU or memory limit is outside the supported range")
        if not 16 <= self.pids_limit <= 4096:
            raise SandboxError("sandbox PIDs limit is outside the supported range")
        if not 1 <= self.max_command_timeout_s <= 86_400:
            raise SandboxError("sandbox maximum command timeout must be 1..86400 seconds")
        if not 4096 <= self.command_output_bytes <= 8_388_608:
            raise SandboxError("sandbox output budget must be 4096..8388608 bytes")
        if type(self.background_enabled) is not bool:
            raise SandboxError("background_enabled must be a boolean")
        if type(self.max_background_jobs) is not int or not 1 <= self.max_background_jobs <= 16:
            raise SandboxError("sandbox maximum background jobs must be 1..16")
        if not 128 * 1024 * 1024 <= self.workspace_max_bytes <= 64 * 1024**3:
            raise SandboxError("sandbox workspace limit must be 128 MiB..64 GiB")
        if not 10_000 <= self.workspace_max_files <= 1_000_000:
            raise SandboxError("sandbox workspace file limit must be 10000..1000000")
        if (len(self.service_ports) > 10
                or any(type(port) is not int or not 1024 <= port <= 65535
                       for port in self.service_ports)
                or len(set(self.service_ports)) != len(self.service_ports)):
            raise SandboxError("sandbox service ports must be up to 10 unique ports in 1024..65535")
        if self.apply not in {"review", "auto"}:
            raise SandboxError("sandbox apply mode must be review or auto")
        object.__setattr__(self, "state_dir", self.state_dir.expanduser().resolve())


def load_policy(env: Mapping[str, str] | None = None,
                dotenv_path: Path | None = None) -> SandboxPolicy:
    """读取 .env 和进程环境，返回完成校验的 SandboxPolicy。

    默认从当前启动目录的 .env 读取；同名进程环境变量覆盖文件值。
    测试传入 env 时可以跳过真实进程环境和默认 .env。
    """
    shell = os.environ if env is None else env
    file_values = (dotenv_values(dotenv_path or Path.cwd() / ".env")
                   if env is None or dotenv_path is not None else {})
    values = {key: value for key, value in file_values.items() if value is not None}
    # update 的顺序决定优先级：环境变量最后写入，因此具有更高优先级。
    values.update(shell)
    return SandboxPolicy(
        image=values.get("STUDY_CODER_SANDBOX_IMAGE", "study-coder-sandbox:general"),
        profile=values.get("STUDY_CODER_SANDBOX_PROFILE", "general"),
        network=values.get("STUDY_CODER_SANDBOX_NETWORK", "none"),
        network_allowlist=tuple(domain.strip().casefold() for domain in
                                values.get("STUDY_CODER_SANDBOX_NETWORK_ALLOWLIST", "").split(",")
                                if domain.strip()),
        network_max_bytes=int(values.get("STUDY_CODER_SANDBOX_NETWORK_MAX_BYTES",
                                         "536870912")),
        trust_fake_ip_dns=values.get("STUDY_CODER_SANDBOX_TRUST_FAKE_IP_DNS", "false").casefold()
        in {"1", "true", "yes", "on"},
        cpus=float(values.get("STUDY_CODER_SANDBOX_CPUS", "2")),
        memory_mb=int(values.get("STUDY_CODER_SANDBOX_MEMORY_MB", "4096")),
        pids_limit=int(values.get("STUDY_CODER_SANDBOX_PIDS_LIMIT", "128")),
        max_command_timeout_s=int(values.get("STUDY_CODER_SANDBOX_MAX_COMMAND_TIMEOUT_S", "1800")),
        command_output_bytes=int(values.get("STUDY_CODER_SANDBOX_COMMAND_OUTPUT_BYTES", "1048576")),
        background_enabled=values.get("STUDY_CODER_SANDBOX_BACKGROUND_ENABLED", "true").casefold()
        in {"1", "true", "yes", "on"},
        max_background_jobs=int(values.get("STUDY_CODER_SANDBOX_MAX_BACKGROUND_JOBS", "4")),
        workspace_max_bytes=int(values.get("STUDY_CODER_SANDBOX_WORKSPACE_MAX_BYTES",
                                           str(1_073_741_824))),
        workspace_max_files=int(values.get("STUDY_CODER_SANDBOX_WORKSPACE_MAX_FILES", "50000")),
        allow_web_search=values.get("STUDY_CODER_SANDBOX_ALLOW_WEB_SEARCH", "false").casefold()
        in {"1", "true", "yes", "on"},
        service_ports=tuple(int(port.strip()) for port in
                            values.get("STUDY_CODER_SANDBOX_SERVICE_PORTS", "").split(",")
                            if port.strip()),
        apply=values.get("STUDY_CODER_SANDBOX_APPLY", "review"),
        state_dir=Path(values.get("STUDY_CODER_SANDBOX_STATE_DIR",
                                  str(Path.home() / ".study-coder" / "sandbox"))),
        exclude=tuple(part.strip() for part in
                      values.get("STUDY_CODER_SANDBOX_EXCLUDE", "").split(",") if part.strip()),
    )
