# repo_memory.py / repo_memory_update.py 说明：仓库长期记忆

> 对应源码：`agent/core/repo_memory.py`、`agent/core/repo_memory_update.py`

这两个文件共同管理仓库级长期记忆：前者定义存储位置和初始化模板，后者把任务结果提炼后写回。

## 关键函数何时调用

| 函数 | 谁调用 | 调用时机 | 作用 |
|---|---|---|---|
| `ensure_repo_memory_initialized()` | `server.py`、`runtime.py` | 第一次识别或同步仓库时 | 仅在记忆不存在时创建模板 |
| `build_repo_memory_namespace()` | server、middleware、更新模块 | 每次读写仓库记忆时 | 生成 owner/repo 隔离的 namespace |
| `get_repo_memory_item()` | `update_repo_memory_from_text()` | 准备更新记忆时 | 读取当前标准 key 的内容 |
| `update_repo_memory_from_text()` | runtime、`MemoryUpdateMiddleware` | 任务产生最终回答后 | 脱敏、提炼并写回稳定事实 |
| `build_updated_repo_memory()` | `update_repo_memory_from_text()` | 旧记忆读取成功且内容安全时 | 生成新 Markdown，本身不写数据库 |

```text
任务开始 -> ensure_repo_memory_initialized() -> 注入上下文
任务结束 -> update_repo_memory_from_text() -> LangGraph Store.put()
```

## 路径与底层键

模型看到的虚拟文件是：

```text
/memories/<owner>/<repo>.md
```

底层 namespace：

```text
("lq-aicoding", "repo-memory", owner, repo)
```

底层 key：

```text
/<owner>/<repo>.md
```

路径和 namespace 都包含 owner/repo，避免不同所有者的同名仓库相互污染。

## 文件分工

`repo_memory.py` 负责：

- 初始 Markdown 模板。
- 本地项目目录生成（`repo_project_dir()`，固定返回 `projects/<repo>`，不再维护 SQLite 映射表）。
- namespace、底层 key 和虚拟路径。
- “不存在才创建”的初始化操作。

`repo_memory_update.py` 负责从 Agent 最终回答中保守提取：

- 技术栈
- 测试命令
- 关键文件
- 已完成能力
- 分支与 PR
- 最近结论

## 安全策略

更新前先进行 Token 脱敏和敏感标记检查。如果最终回答包含敏感配置文件、私钥等标记，会直接跳过整次写回。

## 为什么不用模型总结

当前使用规则提取，是为了不增加模型成本，让逻辑稳定可测试，并坚持“宁可少写，不写错误或敏感信息”。代价是只能识别有限技术栈和常见命令；扩展规则时应同步补测试。

## 读取链路

`server.py` 构建 Agent 前先读取一次记忆并放入 configurable；`ContextInjectionMiddleware` 优先使用缓存，缺失时才直接查询 LangGraph Store。

记忆只是参考。如果它与实时文件、Git 状态或测试结果冲突，必须以实时结果为准。
