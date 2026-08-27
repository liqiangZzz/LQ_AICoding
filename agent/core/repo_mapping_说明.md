# repo_mapping.py 说明：仓库目录映射

> 对应源码：`agent/core/repo_mapping.py`
> 长期记忆部分已拆分到 `agent/core/repo_memory_说明.md`。

## 与长期记忆的关系

仓库映射解决“GitHub 仓库在本机哪个目录”；仓库记忆解决“之前已经确认过哪些稳定事实”。前者对应真实文件系统，后者对应 LangGraph Store。

## 关键函数何时调用

| 函数 | 谁调用 | 调用时机 | 结果 |
|---|---|---|---|
| `discover_repo_mapping()` | `runtime.py`、`server.py` | 同步仓库或构建绑定仓库的 Agent 前 | 返回本地目录、来源和 remote 是否匹配 |
| `remote_matches_repo()` | `discover_repo_mapping()` | 验证数据库映射、默认目录或扫描目录时 | 判断 origin 是否属于目标仓库 |
| `save_clone_mapping()` | `runtime.run_pull_only_task()` | clone 成功以后 | 把已验证目录写入业务 Store |
| `repo_mapping_id()` | `_save_mapping()` | 映射准备持久化时 | 生成跨平台稳定的映射 ID |

```text
runtime/server -> discover_repo_mapping()
               -> 未找到则 clone
               -> save_clone_mapping()
```

## 仓库映射查找顺序

`discover_repo_mapping()` 按以下顺序查找：

1. 读取业务 SQLite 中保存的 active 映射。
2. 验证目录仍存在，并读取 `.git/config` 验证 `origin`。
   - 如果存储的目录落入 `projects/projects` 嵌套路径（如 `projects/projects/<repo>`），视为无效映射并跳过。
3. 检查默认目录 `projects/<repo>`。
4. 扫描 `projects/*` 下一层 Git 仓库并比较 remote。
   - 跳过路径名为 `projects` 的子目录，避免重复扫描嵌套结构。
5. 都未找到时返回默认 clone 目标，但暂不写入数据库。

只有 clone 成功后，`save_clone_mapping()` 才持久化新映射，避免把不存在的目录标记为有效。

## 为什么每次都验证 remote

数据库记录可能因用户移动目录、删除仓库或修改 remote 而过期。只判断目录存在会把同名但不同仓库误认为目标项目，所以映射命中必须同时验证 `.git/config`。

HTTPS、无 `.git` 后缀和 SSH remote 会统一成可比较形式。清洗过程会移除 URL 中可能存在的凭据。
