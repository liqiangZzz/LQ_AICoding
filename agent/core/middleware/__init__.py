"""Agent 中间件层（middleware）。

本目录存放运行在 LangGraph / DeepAgents 各生命周期阶段的中断点式中间件。
这些中间件不实现具体业务逻辑，而是围绕 Agent 的「运行过程」做横向切面：
在工具执行前后、整轮 Agent 开始与结束时，统一注入上下文、清洗参数、
兜底异常、限制运行规模，以及写回长期记忆。

整体设计遵循「前置预防 + 后置兜底」的双层防线思路：

- 前置预防：在工具真正执行前清洗入参、注入必要上下文，提前把模型容易
  犯的路径越界、URL 带敏感信息、参数类型错误等问题拦在门外，并给模型
  返回更明确的中文反馈，减少无效重试。
- 后置兜底：捕获所有未被前置检查挡住的运行时异常，转换为模型可读的
  ToolMessage，让 Agent 有机会自行修正；同时在整轮结束时尝试写回
  仓库级长期记忆，避免新增运行路径时遗漏写回。

各中间件职责（按 Agent 生命周期大致顺序）：

- context_injection.ContextInjectionMiddleware
    仓库级上下文注入。在整轮 Agent 开始前，把仓库标识、记忆文件路径和
    已有记忆内容主动注入为一条 SystemMessage，解决「模型每一轮都从零开始
    理解仓库」的问题。只读不写，且注入的是长期摘要，与实时工具结果冲突
    时以工具结果为准。

- tool_sanitize.SanitizeToolInputsMiddleware
    工具入参清洗。在工具执行前统一清洗高风险参数：绝对路径改写为相对
    工作区路径、拦截 `..` 越界与 `.secrets` 等敏感目录、脱敏带 token 的
    GitHub URL、修正字符串化的数值参数等。是第一道安全防线。

- tool_error.ToolErrorMiddleware
    工具异常处理。捕获工具函数抛出的所有异常，转换为带 error_type、
    error、hint、workspace 字段的结构化 ToolMessage，让模型把错误当作
    观察结果继续推理，避免单次工具失败导致整轮任务 fail；同时把失败状态
    写回前端 run_events 表。是兜底防线。

- run_limits.RunLimitsMiddleware
    单轮运行保护阈值。按任务类型（qa / inspect / sync / analysis /
    planning / review / coding）配置最大工具调用次数与最大运行时长，
    防止费用失控或任务卡在重复循环中。基于 LangGraph 事件流做轻量级
    保护，不依赖具体模型供应商。

- memory_update.MemoryUpdateMiddleware
    仓库级长期记忆写回兜底。在 after_agent / aafter_agent 阶段再次尝试
    提取最后一条 assistant 消息，交由 repo_memory_update 做结构化提炼
    后写回 Store。主写回路径在 runtime.py，本中间件仅作兜底，且对疑似
    包含 .env / api_key / 私钥等敏感信息的内容直接跳过。

协作关系：
    context_injection 负责读入长期上下文，memory_update 负责写出长期记忆；
    tool_sanitize 在工具执行前清洗入参，tool_error 在工具执行后兜底异常；
    run_limits 在整轮运行过程中限制规模。LocalShellBackend 仍是最终
    安全边界，middleware 层负责提前反馈与兜底恢复，二者互为补充。
"""

__all__ = [
    "context_injection",
    "memory_update",
    "run_limits",
    "tool_error",
    "tool_sanitize",
]
