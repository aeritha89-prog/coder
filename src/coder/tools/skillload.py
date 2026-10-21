"""加载可信 SKILL.md；目录发现、格式校验和正文预算均在这里处理。"""

from __future__ import annotations

import hashlib
import re
import stat
import sys
from pathlib import Path
from typing import Any

import yaml

from coder.tools.base import ToolContext, ToolFailure, observation, schema, text_arg
from coder.types import ToolResult

# 源码运行与镜像内安装都以项目目录为基准，不依赖当前工作区或 Windows 盘符。
DEFAULT_SKILL_DIR = Path(__file__).resolve().parents[3] / "skills"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class _MetadataLoader(yaml.SafeLoader):
    """采用安全 YAML 标签，同时拒绝通常会被后一个值覆盖的重复字段。"""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ToolFailure("skill frontmatter keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def skill_digest(body: str) -> str:
    """会话只保存名称和摘要；恢复时据此检测镜像内技能是否已变化。"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _metadata(block: str) -> dict[str, str]:
    try:
        values = yaml.load(block, Loader=_MetadataLoader)
    except yaml.YAMLError as exc:
        raise ToolFailure(f"invalid skill frontmatter YAML: {exc}") from exc
    if not isinstance(values, dict):
        raise ToolFailure("skill frontmatter must be a YAML mapping")
    name, description = values.get("name"), values.get("description")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ToolFailure("skill name must contain 1..64 letters, digits, _ or -")
    if not isinstance(description, str) or not description.strip():
        raise ToolFailure("skill description must be a nonempty string")
    return {"name": name, "description": description.strip()}


def parse_skill(skill_dir: Path) -> tuple[str, str]:
    """验证 UTF-8、独立分隔线和 YAML 字段，保留正文的空行与内部 ---。"""
    try:
        full_content = skill_dir.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolFailure(f"skill file is not UTF-8: {skill_dir}") from exc
    except FileNotFoundError as exc:
        raise ToolFailure(f"skill file does not exist: {skill_dir}") from exc
    except PermissionError as exc:
        raise ToolFailure(f"skill file is not readable: {skill_dir}") from exc
    except IsADirectoryError as exc:
        raise ToolFailure(f"skill path is not a file: {skill_dir}") from exc
    except OSError as exc:
        raise ToolFailure(f"skill file could not be read: {skill_dir}: {exc}") from exc
    lines = full_content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ToolFailure(f"skill frontmatter must start with a standalone --- line: {skill_dir}")
    closing = next((index for index in range(1, len(lines))
                    if lines[index].rstrip("\r\n") == "---"), None)
    if closing is None:
        raise ToolFailure(f"skill frontmatter is missing its closing --- line: {skill_dir}")
    block = "".join(lines[1:closing])
    if not block.strip():
        raise ToolFailure(f"skill frontmatter is empty: {skill_dir}")
    body = "".join(lines[closing + 1:])
    if not body.strip():
        raise ToolFailure(f"skill body is empty: {skill_dir}")
    try:
        _metadata(block)
    except ToolFailure as exc:
        raise ToolFailure(f"{exc}: {skill_dir}") from exc
    return "".join(lines[:closing + 1]), body


class skill_loader:
    name = "skill_loader"
    description = "Activate a trusted skill for the current conversation."
    is_read_only = True

    def __init__(self, dir: Path = DEFAULT_SKILL_DIR) -> None:
        self.dir = dir
        self.skills_dir: list[Path] = []
        self.skills: dict[str, dict[str, str]] = {}
        self.skill_errors: dict[str, str] = {}
        self.discovery_error: str | None = None
        try:
            if not stat.S_ISDIR(self.dir.stat().st_mode):
                raise ToolFailure(f"skill directory is not a directory: {self.dir}")
            self.skills_dir = sorted(self.dir.glob("*/SKILL.md"))
        except (OSError, ToolFailure) as exc:
            self.discovery_error = f"skill directory could not be loaded: {self.dir}: {exc}"
            self._log(self.discovery_error)
            return
        # 同名技能全部拒绝，不能由文件系统遍历顺序决定哪一份生效。
        seen: dict[str, str] = {}
        for skill_dir in self.skills_dir:
            name = skill_dir.parent.name
            try:
                metadata, body = parse_skill(skill_dir)
                fields = _metadata("".join(metadata.splitlines(keepends=True)[1:-1]))
                name = fields["name"]
                folded = name.casefold()
                if folded in seen:
                    previous = seen[folded]
                    self.skills.pop(previous, None)
                    self.skill_errors[previous] = f"duplicate skill name: {name}"
                    raise ToolFailure(f"duplicate skill name: {name}")
                seen[folded] = name
                self.skills[name] = {"description": fields["description"], "body": body}
            except ToolFailure as exc:
                self.skill_errors[name] = str(exc)
                self._log(f"skipped {name}: {exc}")

    @classmethod
    def from_catalog(cls, catalog: object) -> skill_loader:
        """宿主使用固定镜像提供的清单，不重新扫描另一份本地技能目录。"""
        if not isinstance(catalog, dict):
            raise ToolFailure("invalid image skill catalog; rebuild the sandbox image")
        instance = cls.__new__(cls)
        instance.dir = DEFAULT_SKILL_DIR
        instance.skills_dir = []
        instance.skill_errors = {}
        instance.discovery_error = None
        instance.skills = {}
        names: set[str] = set()
        for name, entry in catalog.items():
            if (not isinstance(name, str) or not _NAME.fullmatch(name)
                    or name.casefold() in names or not isinstance(entry, dict)
                    or not isinstance(entry.get("description"), str)
                    or not entry["description"].strip()
                    or not isinstance(entry.get("body"), str) or not entry["body"].strip()):
                raise ToolFailure("invalid image skill catalog; rebuild the sandbox image")
            names.add(name.casefold())
            instance.skills[name] = {"description": entry["description"], "body": entry["body"]}
        return instance

    def catalog(self) -> dict[str, dict[str, str]]:
        return {name: dict(entry) for name, entry in self.skills.items()}

    @staticmethod
    def _log(message: str) -> None:
        # Worker 的 stdout 是 JSON 协议，诊断信息必须写到 stderr。
        print(f"[技能加载] {message}", file=sys.stderr, flush=True)

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def run() -> str:
            if not isinstance(arguments, dict):
                raise ToolFailure("arguments must be an object")
            name = text_arg(arguments, "skill_name")
            if not name.strip():
                raise ToolFailure("skill_name must be a nonempty string")
            if name in self.skills:
                body = self.skills[name]["body"]
                if len(body) > ctx.settings.max_skill_chars:
                    raise ToolFailure(f"skill {name!r} exceeds max_skill_chars "
                                      f"({len(body)} > {ctx.settings.max_skill_chars}); not activated")
                return body
            if name in self.skill_errors:
                raise ToolFailure(f"skill {name!r} could not be loaded: {self.skill_errors[name]}")
            if self.discovery_error is not None:
                raise ToolFailure(self.discovery_error)
            raise ToolFailure(f"{name} skill not found")
        return observation("", self.name, run)

    def schema(self) -> dict[str, Any]:
        descriptions = "\n".join(f"{name}: {entry['description']}"
                                 for name, entry in self.skills.items())
        return schema(self.name, self.description,
                      {"skill_name": {"type": "string", "description": descriptions,
                                      "enum": list(self.skills)}}, required=["skill_name"])
