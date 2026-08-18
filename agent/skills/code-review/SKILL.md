---
name: code-review
description: 审查 Git diff 或 Pull Request 中可能导致真实故障的问题，适用于用户要求只审查、不修改代码的场景。
---

# 代码审查

审查时只关注本次 diff 或用户指定 Pull Request 中可能导致真实故障的问题。

## 审查顺序

1. 先确认审查范围、基准分支和当前 diff，不把旧问题误当成本次回归。
2. 先读取工作区和仓库规则；不存在时调用 `load_default_review_rules`。
3. 有 GitHub PR 编号时调用 `get_github_pull_request_context`，再调用 `get_review_diff_summary`。
4. 追踪变更的入口、调用方和返回值，核对正常路径与异常路径。
5. 检查路径、命令、编码、环境变量和文件权限是否同时兼容 macOS 与 Windows。
6. 记录前调用 `validate_review_finding_location`；校验通过才调用 `add_review_finding`。
7. 检查测试是否覆盖本次改动的故障边界，必要时只运行最小只读验证。
8. 最终报告前调用 `list_review_findings` 重新汇总结构化发现。

## 输出规则

- 不提纯风格建议。
- 每个 finding 必须包含文件、行号、严重程度、标题和具体风险。
- 严重程度使用 `critical`、`high`、`medium`、`low`、`info`。
- 如果没有发现问题，明确说明未发现 finding，并指出未能验证的范围。
- 输出中文，代码标识符和路径保持原样。
