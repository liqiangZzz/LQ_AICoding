# Code Review Skill

用于课程版 Reviewer Agent 的最小技能说明。

审查时只关注本次 diff 中可能导致真实故障的问题：

- 不提纯风格建议。
- 每个 finding 必须包含文件、行号、严重程度、标题和具体风险。
- 严重程度使用 `critical`、`high`、`medium`、`low`。
- 输出中文，代码标识符和路径保持原样。
