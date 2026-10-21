# 多架构官方 Python 镜像索引摘要；依赖升级时集中更新版本和摘要。
# doctor 在任务启动时解析镜像 ID，此后任务使用固定 ID，避免标签漂移。
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ARG DEBIAN_SNAPSHOT=20260924T000000Z
# 构建时可联网安装工具；这不等于运行中的任务容器可以联网。
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PATH=/opt/coder/.venv/bin:$PATH

# 常见 Linux 开发工具；项目特殊工具链应在受信任扩展镜像中添加。
# Debian snapshot 固定软件源时间点；ca-certificates 供 HTTPS 证书校验。
RUN sed -i \
      -e "s|https\?://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https\?://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
    ca-certificates git curl tar build-essential cmake pkg-config \
    nodejs npm default-jdk-headless golang-go rustc cargo ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /opt/coder/pyproject.toml
COPY uv.lock /opt/coder/uv.lock
COPY src /opt/coder/src
# 技能是镜像的可信只读资产，与 Worker 一起固定版本。
COPY skills /opt/coder/skills
# 把当前项目 Worker 安装进镜像；任务运行时不从宿主可写挂载源码。
RUN pip install --no-cache-dir uv==0.12.5 \
    && uv sync --locked --no-dev --project /opt/coder \
    && useradd --uid 10001 --create-home --shell /bin/sh agent \
    && mkdir -p /workspace && chown 10001:10001 /workspace

WORKDIR /workspace
# 与 docker.py 中的 --user 一致，volume 初始目录也归 UID 10001。
USER 10001:10001
