# 说明：diff 解析与 finding 位置校验

> 对应源码：`agent/tools/reviewer_diff.py`
>
> 上层模型工具：`agent/tools/reviewer_tools.py`

## 文件定位

Reviewer 不能只靠自然语言猜测文件名和行号。本文件把 `git diff` 转成稳定的
`DiffSummary`，并判断 finding 是否真的落在本次变更文件和新文件侧变更行中。
它是普通 Python 支撑模块，模型不会直接调用。

## 什么时候调用

| 函数 | 谁调用 | 调用时机 | 返回结果 |
|---|---|---|---|
| `get_local_diff_summary()` | `get_review_diff_summary`、`validate_review_finding_location` | Reviewer 获取范围或校验 finding 时 | `DiffSummary` |
| `parse_unified_diff()` | `get_local_diff_summary()` | `git diff` 成功返回后 | 文件列表和各文件变更行 |
| `validate_finding_location()` | `validate_review_finding_location` | 保存 finding 之前 | `(是否有效, 中文原因)` |
| `_validate_git_ref()` | `get_local_diff_summary()` | 拼接命令之前 | 安全的 base/head，非法输入直接拒绝 |

```mermaid
flowchart LR
    A["Reviewer 子 Agent"] --> B["get_review_diff_summary"]
    B --> C["固定命令 git diff"]
    C --> D["parse_unified_diff"]
    D --> E["DiffSummary：文件 + 变更行"]
    E --> F["分析风险"]
    F --> G["validate_review_finding_location"]
    G --> H{"文件和行号有效？"}
    H -->|是| I["add_review_finding"]
    H -->|否| J["修正位置或仅写入总结"]
```

## 数据结构

- `DiffFile.path`：去掉 `a/`、`b/` 前缀并统一为正斜杠的仓库相对路径。
- `DiffFile.changed_lines`：新文件侧新增或修改的行号；删除行没有新文件行号。
- `DiffSummary.changed_file_paths`：本次 diff 的稳定文件集合。
- `DiffSummary.changed_lines_for(path)`：按标准化路径读取变更行。
- `DiffSummary.raw_diff`：原始文本；上层只向模型返回有长度限制的预览。

## 命令与安全边界

实际命令只有两种形态：

```text
git diff --unified=80 HEAD --
git diff --unified=80 main...feature/review --
```

`base` 和 `head` 只允许 Git ref 常用字符，禁止空格、分号、管道等 Shell 片段；
末尾 `--` 明确结束 ref，避免把输入误解释为路径。命令由本模块固定生成，Reviewer
不能传入任意 Shell 命令。

## macOS 与 Windows 兼容

- 内部路径统一使用 `/`，输入的 `\` 会先标准化。
- 含空格并被 Git 引号包裹的路径通过 `shlex` 解析，不使用平台相关的路径切分。
- 仓库目录继续使用 `/projects/...` 虚拟路径，由 `LocalShellBackend` 映射到宿主机。
- 文本由 Git 和 Python 处理，不依赖 Bash 管道、`sed` 或 Windows 批处理语法。

## 校验限制

- 行号只接受新增或修改行；纯删除问题应记录为文件级 finding，或在总结中说明。
- 每次位置校验会重新读取同一 `base/head` 的 diff，避免把大段原始 diff 由模型原样回传。
- 如果两次调用之间工作区发生变化，校验结果也会变化；Reviewer 是只读任务，正常流程中
  不应主动修改工作区。

## 相关测试

- `tests/test_reviewer_diff.py`：含空格路径、Windows 反斜杠、行号校验、安全 ref 和固定命令。
- `tests/test_github_api_read.py`：Reviewer 读取 PR 上下文时的鉴权、分页和评论端点。
