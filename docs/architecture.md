# 当前架构：Docker 工作区 + 上下文管理

## Agent Loop

问题：模型工具调用必须转为容器中的顺序动作，并将结果返回。边界：`Agent` 只掌管历史、回合、授权和调度；工作区工具经 `DockerRuntime` 转发到 Worker，宿主仅执行可选的 `web_search`；provider JSON 限于 `llm.py`。失败：Docker 预检失败直接终止，模型/协议失败收为 `failed`，达到上限为 `round_limit`，工具预期错误成为 observation。验证：`tests/test_agent.py` 的闭环与拒绝路径及 `tests/integration/test_sandbox_runtime.py` 的真实容器路径。

## 文件工具

问题：改动应可预测。边界：固定 Worker 在容器卷内执行文件工具；所有路径按已解析 `/workspace` 检查，编辑只替换唯一片段，输出有上限。导出经校验后供宿主审阅和应用。失败：零/多命中、非 UTF-8、越界均不写。验证：`tests/test_tools.py` 与 Sandbox 导出测试。

## 权限与命令

问题：副作用需要明确授权。边界：权限判断在工具执行前；one-shot 默认拒绝；容器 Worker 用 `/bin/sh -c` 执行命令并设超时与资源上限，宿主不运行模型命令。失败：拒绝和非零退出反馈模型。验证：`tests/test_agent.py` 与 `tests/integration/test_sandbox_runtime.py`。

## 模型与会话

问题：兼容 API 与内部状态解耦。边界：Adapter 拼接完整 tool-call JSON 后才返回；会话文件保存在宿主 Sandbox 状态目录，保存历史但不持久化权限。失败：无效 JSON 是模型协议错误，网络临时错误有限重试。验证：`tests/test_llm.py`。

## M4：上下文管理

问题：长任务中的原始工具输出会超过模型窗口，直接删旧消息还可能留下孤立的 tool result。

边界：`Agent.history` 是完整原始记录；`ContextManager.build_view()` 只为本轮模型调用生成视图。工具输出裁剪只改视图；摘要以完整 assistant tool-call/result 组为单位，并缓存已摘要的前缀。CLI 的摘要模型使用非流式调用，避免把内部摘要显示给用户。

失败路径：模型摘要失败会退回规则摘要；当前用户任务加最近完整交互仍超过估算窗口时抛出 `ContextError`，Agent 收为明确的失败结果。token 估算与 Ollama 服务端的真实 tokenizer/窗口可能有偏差。

验证证据：`tests/test_context.py` 覆盖裁剪、摘要、增量缓存、失败降级、hard collapse、原始会话保留、工具消息成组校验和超窗失败。
