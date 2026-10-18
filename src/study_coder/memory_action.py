"""显式记忆管理请求的完成门槛：成功依据来自本轮工具及数据库。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from study_coder.memory import MemoryStore
from study_coder.types import ToolCall, ToolResult

_PREFIX = r"^(?:(?:请你|请|麻烦你|麻烦|帮我|替我)\s*)?"
_POLICY_CHANGE = re.compile(
    _PREFIX + r"(?:(?:这条|那条|该条|这个偏好|这条记忆)\s*)?"
    r"(?:(?:不用|不要用|别用|取消)\s*(?:always|常驻)|"
    r"(?:改为|改成|设为|设置为)\s*(?:always|on_demand|常驻|按需(?:召回)?))"
    r"[了吧。！!\s]*$", re.IGNORECASE)


@dataclass
class MemoryAction:
    """仅识别直接的保存/删除/策略修改命令，不从历史助手文字猜条目 ID。

    分类只决定是否要求工具确认，不会自动写数据库，也不验证模型对事实的
    语义理解。真实参数、授权、版本与 evidence 仍由现有工具入口处理。
    """

    tool: str
    policy: str | None = None
    retries: int = 0
    denied: bool = False
    receipts: list[tuple[str, int | None]] = field(default_factory=list)

    @classmethod
    def from_request(cls, text: str) -> MemoryAction | None:
        text = text.strip()
        # 先识别否定/修改，避免把“不要记住”当作新增记忆请求。
        if re.match(_PREFIX + r"(?:(?:忘记|忘掉)(?:我|关于我|这条|那条|该条|这项|所有记忆|全部记忆)|"
                    r"删除.{0,80}记忆|清除.{0,80}记忆)", text) or re.match(
                r"^(?:please\s+)?forget\s+(?:my\b|this memory\b|that memory\b|all memories\b)",
                text, re.IGNORECASE):
            return cls("memory_forget")
        policy_change = _POLICY_CHANGE.fullmatch(text)
        # 条目操作与开发记忆模块要区分；“修改记忆机制”不能限制成只调用记忆工具。
        entry_target = re.search(r"(?:这条|那条|该条|这项|那项)(?:记忆|偏好)|记忆(?:条目|记录)", text)
        policy_target = (re.search(r"载入策略|加载策略|load_policy", text, re.IGNORECASE)
                         and not re.search(r"代码|模块|机制|实现|函数|配置|schema", text, re.IGNORECASE))
        explicit_change = (re.match(_PREFIX + r"(?:修改|更新|调整|把|将|设置|取消)", text)
                           and (entry_target or policy_target))
        if policy_change or explicit_change:
            # 否定 always 表示按需，不能按字符串中出现了 always 就设成常驻。
            if re.search(r"(?:不用|不要|别|取消).*?(?:always|常驻)|on_demand|按需", text, re.IGNORECASE):
                policy = "on_demand"
            elif re.search(r"(?<![A-Za-z0-9_])always(?![A-Za-z0-9_])|常驻", text, re.IGNORECASE):
                policy = "always"
            else:
                policy = None
            return cls("memory_remember", policy)
        if (re.match(_PREFIX + r"(?:记住|记下|牢记)", text)
                or re.match(r"^(?:please\s+)?remember\s+(?:my\b|that\b|I\b|me\b|this preference\b|this rule\b)",
                            text, re.IGNORECASE)
                or re.match(_PREFIX + r"(?:保存|存储|记录).{0,80}(?:长期记忆|到记忆|进记忆)", text)):
            return cls("memory_remember")
        return None

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        """只接收执行器刚返回的回执，助手文本里的 JSON 和 mem_* 不进入这里。"""
        if call.name != self.tool:
            return
        if result.is_error:
            if result.content == f"{call.name} denied in interactive mode" or result.content == f"{call.name} denied in plan mode":
                self.denied = True
            return
        try:
            value = json.loads(result.content)
            if not isinstance(value, dict):
                return
            if self.tool == "memory_remember":
                identifier, revision = value.get("id"), value.get("revision")
                if value.get("saved") is not True or type(revision) is not int or revision < 1:
                    return
            else:
                identifier, revision = call.arguments.get("id"), None
                if value.get("deleted") is not True:
                    return
            if not isinstance(identifier, str) or not re.fullmatch(r"mem_[0-9a-f]{16}", identifier):
                return
            # 一个条目只保留本轮最新回执，避免旧版本与新版本相互冒充。
            self.receipts = [(key, rev) for key, rev in self.receipts if key != identifier]
            self.receipts.append((identifier, revision))
        except (TypeError, ValueError):
            return

    def confirmation(self, store: MemoryStore) -> str | None:
        """再次读库核对版本和实际策略；查询结果、自动情景归档不能证明写入。"""
        confirmed: list[str] = []
        try:
            for identifier, revision in self.receipts:
                entry = store.get(identifier)
                if self.tool == "memory_forget":
                    if entry is None:
                        confirmed.append(f"已删除记忆，ID：{identifier}。")
                    else:
                        return None
                elif (entry is not None and entry.revision == revision
                      and (self.policy is None or entry.load_policy == self.policy)):
                    confirmed.append(f"已保存记忆：{entry.content}\n类型：{entry.kind}；"
                        f"载入策略：{entry.load_policy}；优先级：{entry.priority}；"
                        f"状态：{entry.status}；版本：{entry.revision}；ID：{entry.id}。")
                    if entry.status == "candidate":
                        confirmed.append("这条记忆仍是候选，尚未自动生效。")
                else:
                    return None  # 批次中任一已提交条目无法核对，不能将整批报告为完成。
        except (ValueError, OSError, sqlite3.Error):
            return None  # 无法核实就不能把已有回执当作当前状态。
        return "\n".join(confirmed) if confirmed else None

    @property
    def can_retry(self) -> bool:
        return not self.denied and self.retries < 2

    def correction(self) -> str:
        self.retries += 1
        policy = f" The requested load_policy is {self.policy}." if self.policy else ""
        return (f"Memory operation unconfirmed: no current successful {self.tool} receipt "
                "matches the database. Do not claim it succeeded or invent an id/revision. "
                f"Use {self.tool} for the current request; read/search first when updating."
                + policy)

    def failure(self) -> str:
        action = "保存或修改" if self.tool == "memory_remember" else "删除"
        return f"本轮未能确认长期记忆已{action}：工具执行及数据库核验未确认成功。"
