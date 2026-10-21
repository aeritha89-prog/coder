import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from coder.tools.base import ToolContext, ToolFailure, observation, schema, text_arg
from coder.types import ToolResult

TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
WORKDIR = Path.cwd()
TASKS_DIR = WORKDIR / ".tasks"
TASK_STATE_MAX_CHARS = 2_000


def _task_id_arg(arguments: dict[str, Any]) -> str:
    """缺少参数、类型错误和非法 ID 都转为可解释的工具失败。"""
    task_id = text_arg(arguments, "task_id")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ToolFailure("task_id must match task_ followed by 8 hexadecimal characters")
    return task_id


def _dependency_ids(value: Any) -> list[str]:
    """先验证每个元素，再去重，避免字典等不可哈希元素引发 TypeError。"""
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not TASK_ID_PATTERN.fullmatch(item) for item in value
    ):
        raise ToolFailure("dependencies must be a list of valid task IDs")
    return list(dict.fromkeys(value))

@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]


def _validate_task(task: Task) -> None:
    """dataclass 不做运行时类型检查；创建、读取和保存共用这里的校验。"""
    if not isinstance(task.id, str) or not TASK_ID_PATTERN.fullmatch(task.id):
        raise ToolFailure("invalid task ID")
    if not isinstance(task.subject, str) or not task.subject.strip():
        raise ToolFailure("task subject must be a nonempty string")
    if not isinstance(task.description, str):
        raise ToolFailure("task description must be a string")
    if not isinstance(task.status, str) or task.status not in ("pending", "in_progress", "completed"):
        raise ToolFailure("invalid task status")
    if task.owner is not None and (not isinstance(task.owner, str) or not task.owner.strip()):
        raise ToolFailure("task owner must be null or a nonempty string")
    dependencies = _dependency_ids(task.blockedBy)
    if task.id in dependencies:
        raise ToolFailure("task cannot depend on itself")

class TaskStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _root(self, create: bool = False) -> Path:
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve()
        if not root.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Task store escapes the workspace")
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def is_dep(self,task: Task) -> list[Task]:
        dep_of = []
        for all_task in self.list():
            if task.id in all_task.blockedBy:
                dep_of.append(all_task)
        return dep_of

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        subject = text_arg({"subject": subject}, "subject").strip()
        description = text_arg({"description": description}, "description", allow_empty=True)
        if not subject:
            raise ValueError("Task subject cannot be empty")

        self._root(create=True)
        for _ in range(100):
            task = Task(
                id=f"task_{secrets.token_hex(4)}",
                subject=subject,
                description=description,
                status="pending",
                owner=None,
                blockedBy=[],
            )
            try:
                with self._path(task.id, create_root=True).open(
                    "x", encoding="utf-8"
                ) as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue
        raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """Return whether task_id transitively depends on target_id."""
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str,
                            add_blocked_by: list[str]) -> Task:
        dependencies = _dependency_ids(add_blocked_by)

        task = self.load(task_id)
        if task.status != "pending" or task.owner is not None:
            raise ValueError(
                f"Task {task_id} dependencies can only be updated while "
                "pending and unowned"
            )
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not self.exists(dependency):
                raise ValueError(f"Dependency not found: {dependency}")
            if self._depends_on(dependency, task_id):
                raise ValueError(
                    f"Dependency cycle detected: {task_id} -> {dependency}"
                )

        # 完整验证成功后才替换；[] 明确表示清空依赖。
        task.blockedBy = dependencies
        self.save(task)
        return task

    def delete_task(self, task_id: str):
        task = self.load(task_id)
        if self.is_dep(task):
            raise ToolFailure("task is a dependency of another task")
        self._path(task_id).unlink()

    def save(self, task: Task) -> None:
        _validate_task(task)
        self._path(task.id, create_root=True).write_text(
            json.dumps(asdict(task), indent=2),
            encoding="utf-8",
        )

    def load(self, task_id: str) -> Task:
        try:
            data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
            fields = {"id", "subject", "description", "status", "owner", "blockedBy"}
            if not isinstance(data, dict) or set(data) != fields:
                raise ToolFailure("expected exactly the Task fields")
            task = Task(**data)
            _validate_task(task)
            if task.id != task_id:
                raise ToolFailure("file ID does not match its filename")
            return task
        except (ValueError, UnicodeError) as exc:
            raise ToolFailure(f"Invalid task file {task_id}: {exc}") from exc

    def list(self) -> list[Task]:
        if not self.directory.exists():
            return []
        root = self._root()
        return [self.load(path.stem)
                for path in sorted(root.glob("task_*.json"))]


TASKS = TaskStore(TASKS_DIR)

def incomplete_dependencies(task: Task) -> list[str]:
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if TASKS.load(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError):
            incomplete.append(dependency)
    return incomplete

def incomplete_tasks() -> dict[str, str]:
    incomplete = {}
    for task in TASKS.list():
        if task.status != "completed":
            incomplete[task.id] = task.status
    return incomplete


def task_state() -> str:
    """生成模型每轮可见的短摘要；详细描述通过 get_task 按需读取。

    优先显示进行中、可认领、被阻塞的任务；保留总数与省略数，避免截断
    JSON 或让模型误以为省略的任务不存在。标题始终作为 JSON 数据编码。
    """
    tasks = TASKS.list()
    if not tasks:
        return ""
    by_id = {task.id: task for task in tasks}
    def blocked(task: Task) -> bool:
        return any(dep not in by_id or by_id[dep].status != "completed" for dep in task.blockedBy)
    unfinished = sorted(
        (task for task in tasks if task.status != "completed"),
        key=lambda task: (0 if task.status == "in_progress" else 2 if blocked(task) else 1, task.id),
    )
    payload: dict[str, Any] = {"counts": {status: sum(task.status == status for task in tasks)
                          for status in ("pending", "in_progress", "completed")},
               "tasks": [], "omitted": len(unfinished)}
    for task in unfinished[:20]:
        entry = {"id": task.id, "subject": task.subject[:80], "status": task.status,
                 "blocked": blocked(task), "blockedBy": task.blockedBy[:4],
                 "more_dependencies": max(0, len(task.blockedBy) - 4)}
        payload["tasks"].append(entry)
        payload["omitted"] -= 1
        if len(json.dumps(payload, ensure_ascii=False)) > TASK_STATE_MAX_CHARS:
            payload["tasks"].pop()
            payload["omitted"] += 1
            break
    return json.dumps(payload, ensure_ascii=False)

class create_task:
    name = 'create_task'
    description = 'Create a new task'
    is_read_only = False

    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            subject = text_arg(arguments, "subject")
            description = text_arg({"description": arguments.get("description", "")},
                                   "description", allow_empty=True)
            try:
                task = TASKS.create(subject, description)
            except (ValueError, OSError) as e:
                raise ToolFailure(f"Error creating task: {e}")
            return f"created task {task.id} successfully"
        return observation('',self.name, run)


    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={'subject': {'type': 'string','description':'The subject of the task'}, 'description': {'type': 'string','description':'The description of the task'}},required=['subject'])

class update_task_dependencies:
    name = 'update_task_dependencies'
    description = 'set the dependencies of a task. the dependencies will be updated if were set before.'
    is_read_only = False

    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            task_id = _task_id_arg(arguments)
            add_blocked_by = _dependency_ids(arguments.get('add_blocked_by'))
            try:
                TASKS.update_dependencies(task_id, add_blocked_by)
            except ValueError as e:
                raise ToolFailure(f"Error updating task dependencies: {e}")
            return f"updated task {task_id} dependencies successfully"
        return observation('',self.name, run)
    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={'task_id': {'type': 'string','description':'The ID of the task'}, 'add_blocked_by': {'type': 'array','items': {'type': 'string','pattern': TASK_ID_PATTERN.pattern},'description':'The task IDs of dependencies; [] clears all dependencies'}},required=['task_id','add_blocked_by'])


class get_task:
    name = 'get_task'
    description = 'Get the details of a task'
    is_read_only = True

    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            task_id = _task_id_arg(arguments)
            try:
                task = TASKS.load(task_id)
            except ValueError as e:
                raise ToolFailure(f"Error loading task: {e}")
            return f"task {task_id} details: {asdict(task)}"
        return observation('',self.name, run)
    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={'task_id': {'type': 'string','description':'The ID of the task'}},required=['task_id'])

class task_list:
    name = 'task_list'
    description = 'List all tasks'
    is_read_only = True
    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            tasks = TASKS.list()
            return f"tasks: {[asdict(task) for task in tasks]}"
        return observation('',self.name, run)
    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={},required=[])


class claim_task:
    name = 'claim_task'
    description = 'Claim a task'
    is_read_only = False
    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            task_id = _task_id_arg(arguments)
            try:
                task = TASKS.load(task_id)
            except ValueError as e:
                raise ToolFailure(f"Error loading task: {e}")
            if task.status != "pending":
                raise ToolFailure(f"Task {task_id} is {task.status}, cannot claim")
            dependencies = incomplete_dependencies(task)
            if dependencies:
                raise ToolFailure(f"Blocked by: {dependencies}")
            task.owner = 'agent'
            task.status = "in_progress"
            TASKS.save(task)
            return f"claimed task {task_id} successfully"
        return observation('',self.name, run)
    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={'task_id': {'type': 'string','description':'The ID of the task'}},required=['task_id'])

class complete_task:
    name = 'complete_task'
    description = 'Complete a task'
    is_read_only = False
    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            task_id = _task_id_arg(arguments)
            try:
                task = TASKS.load(task_id)
            except ValueError as e:
                raise ToolFailure(f"Error loading task: {e}")
            if task.status == "pending":
                raise ToolFailure(f"Task {task_id} is {task.status}, cannot complete before you claim it")
            if task.status == "completed":
                raise ToolFailure(f"Task {task_id} is {task.status}, cannot complete again")
            task.status = "completed"
            TASKS.save(task)
            return f"completed task {task_id} successfully"
        return observation('',self.name, run)
    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={'task_id': {'type': 'string','description':'The ID of the task'}},required=['task_id'])


class delete_task:
    name = 'delete_task'
    description = 'Delete a task'
    is_read_only = False
    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            task_id = _task_id_arg(arguments)
            TASKS.delete_task(task_id)
            return f"deleted task {task_id} successfully"
        return observation('',self.name, run)
    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={'task_id': {'type': 'string','description':'The ID of the task'}},required=['task_id'])

class reset_task_progress:
    name = 'reset_task_progress'
    description = 'Reset the progress of a task'
    is_read_only = False
    def execute(self,arguments:dict[str,Any],ctx:ToolContext) -> ToolResult:
        def run():
            task_id = _task_id_arg(arguments)
            try:
                task = TASKS.load(task_id)
            except ValueError as e:
                raise ToolFailure(f"Error loading task: {e}")
            if task.status == "pending":
                raise ToolFailure(f"Task {task_id} is pending, cannot reset progress")
            tasks = TASKS.is_dep(task)
            for t in tasks:
                if t.status != "pending":
                    raise ToolFailure(f"Task {t.id} depends on task {task_id}, which is {t.status}, cannot reset progress")
            task.status = "pending"
            task.owner = None
            TASKS.save(task)
            return f"reset task {task_id} progress successfully"
        return observation('',self.name, run)
    def schema(self) -> dict[str,Any]:
        return schema(self.name, self.description,properties={'task_id': {'type': 'string','description':'The ID of the task'}},required=['task_id'])
