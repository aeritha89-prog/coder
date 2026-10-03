# SkillLoader 修复说明

## 1. 本次修复范围

修复技能发现、frontmatter/YAML 校验、宿主与镜像目录一致性、消息顺序、活动技能去重、独立预算、上下文压缩、会话恢复以及空目录注册问题。
工作区工具和命令仍由 DockerRuntime 执行。技能是可信镜像中的只读资产，模型无法通过工作区写文件来替换它。

## 2. 目录与版本一致性

原先技能目录写死为 Windows 路径，宿主能发现技能，Linux Worker 却找不到；镜像也没有复制 skills。

现在的处理：

1. `tools/skillload.py` 的 `DEFAULT_SKILL_DIR` 从当前源码位置定位项目根目录下的 `skills`，不依赖当前工作目录。
2. `sandbox/images/general.Dockerfile` 增加 `COPY skills /opt/study-coder/skills`。
3. `DockerRuntime.start()` 先由 doctor 取得不可变 image ID，再调用 `_image_skill_catalog()`，从该镜像启动一次短命、无网络、只读、非 root 的清单读取进程。
4. 清单包括名称、description 和完整 body，保存在任务记录中；CLI 将 `runtime.skill_catalog` 交给 `default_tools()`。
5. `skill_loader.from_catalog()` 生成宿主的模型 schema；Worker 从同一镜像目录读取技能。Agent 还核对 Worker 返回正文是否等于清单正文，不一致时拒绝激活。

因此，启动任务不会重新扫描一份可能已经变化的宿主目录。新增或修改技能后，需要重新构建镜像；已启动的任务继续使用原 image ID。

## 3. frontmatter、文件发现和名称校验

`parse_skill()` 仍返回 `(metadata, body)`，供原有阅读方式继续使用。现在依次验证：

- 文件可读且是 UTF-8，兼容 BOM；文件、权限、编码和其他读取异常有独立错误信息。
- 第一行是独立 `---`，后续存在独立的结束 `---`，不接受 `---invalid`。
- metadata 和正文均非空，正文开头空行和内部 Markdown 分隔线保留。
- metadata 使用基于 PyYAML `SafeLoader` 的 `_MetadataLoader` 解析，只接受安全 YAML 标签，并拒绝重复映射字段。
- YAML 根节点必须是映射；`name` 必须为 1..64 个字母、数字、下划线或连字符，且首字符为字母或数字；`description` 必须是非空字符串。

发现范围从 `*/*.md` 改成排序后的 `*/SKILL.md`，同目录 README 不再覆盖技能。清单使用 YAML 的 name，不再把目录名当作唯一标识。
名称按大小写折叠检查重复，所有冲突副本均拒绝，避免由遍历顺序决定哪个版本生效。单个损坏文件被跳过，正常文件保留；错误写入 stderr，镜像清单诊断也在宿主启动日志中显示。

PyYAML 安全加载机制参考：[官方文档](https://pyyaml.org/wiki/PyYAMLDocumentation)。依赖和 uv.lock 已同步更新。

## 4. 工具调用顺序和成功激活

原先 `_record_tool_result()` 每收到 skill_loader 结果就向 history 插入 system，同一轮存在后续工具调用时会形成：

```text
assistant(tool_calls=[skill, read]) → tool(skill) → system(skill正文) → tool(read)
```

这不符合项目要求的连续工具结果组。

现在 `_record_tool_result()` 只记录 tool 结果和日志。`_activate_skill()` 验证正文后更新独立的 `active_skills`，不修改消息排列：

```text
持久历史：assistant(tool_calls=[skill, read]) → tool(简短激活确认) → tool(read)
下一轮输入：基础 system → 活动技能 system → 持久历史的模型视图
```

失败结果不激活；正文与可信清单不符、字符预算不足或上下文预算不足，也作为工具错误交还模型。
检查点在整组 tool 结果补齐后保存；中断时先补齐当前和未执行调用的失败结果，再保存检查点。

## 5. 正文预算与去重

配置新增：

```dotenv
STUDY_CODER_MAX_SKILL_CHARS=40000
STUDY_CODER_MAX_ACTIVE_SKILL_CHARS=60000
```

- 单个正文超过 max_skill_chars：Worker 和 Agent 都会拒绝，不截断后继续激活。
- 全部活动正文超过 max_active_skill_chars：拒绝新技能，已有状态保留。
- 系统提示、候选活动技能和工具 schema 的估算 token 达到 context_window_tokens 的 50%：拒绝激活，为普通历史和回复留出空间。
- 两个字符预算必须为 1..1000000 的整数；它们独立于 max_tool_output_chars。

成功时完整正文只保存在 `active_skills[name]`。ToolResult、工具历史和终端工具结果日志保存简短确认。
重复加载同名技能返回 already active，字典中仍只有一份；`/clear` 同时清空历史、摘要缓存和活动技能。

## 6. 压缩与工具 schema 预算

`ContextManager.build_view()` 新增关键字参数 `active_skills` 和 `tool_schemas`，旧调用方式保持可用。
`_split()` 把活动技能放进不可压缩的系统前缀；旧历史的摘要只处理普通消息组。

原样视图、工具结果裁剪、历史摘要和最终压缩四个阶段，均计入工具 schema 的估算 token。固定前缀与 schema 本身已超过窗口阈值时明确报错，不能靠压缩历史解决。

估算仍使用现有字符启发式，不等同于模型 tokenizer；也不会替用户修改 Ollama 服务端窗口。

## 7. 会话保存、恢复和旧版本迁移

`save_session()` 写入 version 2，新增 active_skills 字段，格式为：

```json
{"active_skills": {"example": "64位SHA-256摘要"}}
```

Agent 的 `skill_state()` 生成该映射。活动正文不重复写进会话文件。
任务检查点、任务结束保存和 `--session` 命名会话都保存这份状态。

`load_session_state()` 接受 v1/v2，并返回 `(history, skills)`。原来的 `load_session()` 消息读取 API 保留，但 CLI 续接使用新接口，再调用 `Agent.restore_session()`。

恢复步骤：

1. 重建历史消息，验证完整工具调用组。
2. 从当前可信镜像清单按名称取回正文。
3. 比较 SHA-256，再检查字符和上下文预算。
4. 全部通过才发布恢复后的历史和活动状态。

技能缺失或正文变化会明确拒绝恢复；使用原镜像或换一个新会话名称继续。
对于 v1 历史里嵌入的旧技能 system 消息：只有正文与当前可信清单完全一致时才恢复成活动状态；失败/截断内容不被激活。旧 system 消息移除，匹配的 tool 正文改成简短确认，使结果组恢复连续，避免重复注入。

## 8. 空目录与诊断

无有效技能时，`default_tools()` 不注册 skill_loader，不向模型发送空 enum。
目录故障和损坏技能有 `[技能加载]` 诊断；其他文件、命令和服务工具照常注册。
旧镜像缺少清单接口时，启动明确要求运行 `study-coder sandbox build`。

## 9. 阅读位置与验证

建议阅读顺序：

1. `src/study_coder/tools/skillload.py`：解析、发现、清单、预算。
2. `sandbox/images/general.Dockerfile`、`sandbox/docker.py` 的 `_image_skill_catalog()` 和 `start()`：可信资产与 image ID。
3. `tools/__init__.py`、`cli.py`：清单注册与会话装配。
4. `agent.py` 的 `_activate_skill()`、`skill_state()`、`restore_session()` 和 `run()`：状态、消息和生命周期。
5. `context.py` 的 `_split()`、`pinned_tokens()`、`build_view()`：注入与预算。
6. `session.py`：v2 保存和 v1/v2 读取。

测试：

```powershell
uv run pytest --ignore=tests/integration -q
uv run ruff check src tests
uv run mypy
uv run study-coder sandbox build
uv run pytest tests/integration/test_sandbox_runtime.py -q
```

`test_skillload.py` 覆盖边界、YAML 字段、重复名称、README 排除、文件错误、参数错误、空清单和字符预算。
`test_skill_activation.py` 覆盖多调用顺序、完整检查点、重复加载、失败不激活、版本核对、预算、摘要保留、会话恢复和 v1 迁移。
真实 Docker 集成测试验证镜像技能清单与构建源一致、Worker 返回完整正文、多工具调用连续、模型输入只注入一份正文，以及技能目录不可写。
模型行为用 ScriptedLLM 验证，没有调用真实 Ollama/OpenAI 提供商；这不证明具体模型一定会主动选择技能。

### 本次验证结果（2026-09-30）

- 单元测试：133 passed。
- 包含真实 Docker 集成验收的完整测试：134 passed，未跳过集成测试。
- Ruff：All checks passed。
- mypy：29 个源码文件检查通过。
- 沙箱镜像已重建，sandbox doctor 通过。新任务使用新镜像，已启动任务保持原固定镜像版本。
- 未连接真实 Ollama/OpenAI 模型；尚未验证具体模型的自主技能选择行为。
