# 本地工作区记忆

当前项目同时支持 macOS 和 Windows，不固定使用某一个操作系统的绝对路径。
工作区根目录由当前宿主机和环境变量共同决定，例如：

- macOS：`~/ai_workspace`，也可以通过 `AI_WORKSPACE_ROOT_MACOS` 或 `LOCAL_SHELL_WORKSPACE_MACOS` 修改。
- Windows：`E:/ai_workspace` 等本机目录，也可以通过 `AI_WORKSPACE_ROOT_WINDOWS` 或 `LOCAL_SHELL_WORKSPACE_WINDOWS` 修改。

`LOCAL_SHELL_PLATFORM=auto` 时会自动选择当前宿主机的平台配置；也可以显式填写
`macos` 或 `windows`，但不能在 macOS 上选择 Windows 配置，反之亦然。

Agent 不应依赖上面的宿主机绝对路径。工具调用统一使用 `/projects`、`/skills`、
`/policies` 等虚拟路径，由 `LocalShellBackend` 映射到当前平台的真实工作区。

工作区包含若干固定用途的子目录。
本文件只记录工作区事实，不承载强制行为规则；具体工具权限、读写边界和执行规范由系统提示词与后端权限控制。

## 目录语义

- `/projects/`：GitHub 仓库克隆目录，真实业务项目通常位于 `/projects/仓库名`。
- `/skills/`：DeepAgents 原生 skill 目录，Agent 运行时通过 `/skills` 虚拟路径读取。
- `/runtimes/`：共享运行环境目录，例如 Python 虚拟环境、Node 或其他项目运行时。
- `/policies/`：编码规范、审查规范和安全规范目录。
- `/reviews/`：代码审查、分析结果和历史评审资料目录。
- `/logs/`：工作区级运行日志目录，用于排查 Agent 或项目运行过程。
- `/tmp/`：临时文件目录，用于短期中间产物。
- `.secrets/`：Git AskPass 等敏感辅助文件目录，不对 Agent 开放。。
- `.ai_coding_workspace.json`：工作区元信息文件，用于识别本地工作区状态。

## 路径使用原则

- 文档、提示词和工具参数优先使用虚拟路径，不写死 `E:/...` 或 `/Users/...`。
- 真实根目录以运行时配置为准，未配置时默认使用当前用户目录下的 `ai_workspace`。
- macOS 和 Windows 共享相同的虚拟目录语义，差异只由本地后端负责适配。
