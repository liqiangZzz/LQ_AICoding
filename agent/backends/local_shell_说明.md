# LocalShellBackend 源码分析

> 对应源码：`agent/backends/local_shell.py`

> 本文档详细解析 `agent/backends/local_shell.py` 中 `LocalShellBackend` 类的设计、流程、核心方法和安全机制。

---

## 1. 整体定位

`LocalShellBackend` 继承自 `deepagents.backends.sandbox.BaseSandbox`，实现了 DeepAgents 定义的文件操作和命令执行协议。它负责：

- **虚拟路径映射**：DeepAgents 文件协议使用 `/projects`、`/skills`、`/policies` 等逻辑路径，后端将它们映射到宿主机工作区目录。这是“路径抽象”而不是强隔离：当前文件接口可解析工作区根目录下的任意路径，只会拒绝越出 `self.root` 的路径。
- **命令安全执行**：对 Agent 提交的命令进行危险性检查、路径越界拦截和虚拟路径转换，并注入 Git 认证信息。
- **文件 I/O 控制**：提供读取、写入、编辑、搜索、上传下载等文件操作，并强制限定修改范围。
- **跨平台运行环境**：根据 `LOCAL_SHELL_PLATFORM` 使用 macOS 或 Windows 的 shell、路径和 Python 虚拟环境约定。
- **Git 非交互认证**：通过 `GIT_ASKPASS` 脚本注入 GitHub Token，避免弹窗或依赖全局凭据。
- **只读任务模式**：`read_only` 开启后只放行查看类命令和必要的只读 Git 子命令，所有文件写入接口直接拒绝，适用于代码审查等不应改动的场景。

### 关键函数何时调用

| 函数/方法 | 谁调用 | 调用时机 | 作用 |
|---|---|---|---|
| `LocalShellBackend.__init__()` | `server.ensure_backend_for_thread()`、runtime 轻量任务 | thread 第一次需要工作区时 | 校验平台并创建受控目录结构 |
| `execute()` | DeepAgents 原生 `execute` 工具 | 模型请求执行命令时 | 校验、改写并执行受控命令 |
| `read/write/edit/ls/glob/grep()` | DeepAgents 文件工具 | 模型读写或搜索文件时 | 转换虚拟路径并执行权限检查 |
| `run()` | runtime 的 Git 同步逻辑 | 后端主动执行 Git 命令时 | 支持指定 cwd，返回 `CommandResult` |
| `_prepare_command()` | `execute()`、`run()` | 命令通过安全校验后 | 替换虚拟路径并注入 Git AskPass |
| `_execution_env()` | 子进程执行前 | 每次命令调用 | 注入 venv、PATH 和认证环境变量 |

```text
模型文件工具 -> read/write/edit/... -> _resolve_virtual_path()
模型命令工具 -> execute() -> 安全校验 -> _prepare_command() -> subprocess
runtime Git   -> run()     -> cwd 校验  -> _prepare_command() -> subprocess
```

---

## 2. 初始化流程（`__init__` 与 `_ensure_layout`）

实例化时，后端会完成以下准备工作：

1. **沙箱类型与平台校验**
   读取 `SANDBOX_TYPE`（默认 `local_shell`），若不是 `local_shell` 直接抛 `ValueError`；当前版本只支持本地 shell 后端。
   随后通过 `resolve_local_shell_platform()` 解析 `LOCAL_SHELL_PLATFORM`，并与 `host_platform()` 比对，若与宿主机平台不一致则抛 `RuntimeError`，避免在 macOS 上误用 Windows shell 约定（或反之）。

2. **确定工作区根目录**
   优先级：调用参数 > 环境变量 `LOCAL_SHELL_WORKSPACE` > 项目默认配置 `WORKSPACE_ROOT`。
   传入的可以是 `Workspace` 对象、字符串或路径；最终统一展开为 `self.root`。所有后续路径都基于此根目录进行解析。

3. **规划工作区子目录**
   `projects/skills/policies/reviews/runtimes/tmp/logs/secrets` 都固定挂在 `self.root` 下。
   `projects` 保持固定，使 DeepAgents 文件工具、Shell 默认 cwd 和仓库映射始终使用同一路径。
   共享 Python venv 路径由 `LOCAL_SHELL_SHARED_PYTHON_VENV` 配置（默认 `runtimes/python/default/.venv`），并经 `_resolve_configured_subpath()` 安全解析——拒绝绝对路径和 `..`，确保配置值不会逃出工作区。

4. **创建子目录结构（`_ensure_layout`）**
   在根目录下创建 `projects/`、`skills/`、`policies/`、`reviews/`、`runtimes/`、`tmp/`、`logs/`、`secrets/` 以及 venv 的父目录。这些目录分别对应虚拟路径 `/projects`、`/skills` 等。

5. **初始化共享 Python 虚拟环境（可选）**
   如果环境变量 `LOCAL_SHELL_CREATE_PYTHON_VENV` 开启（默认关闭），会在配置的 venv 路径下创建虚拟环境，并将 macOS 的 `bin/` 或 Windows 的 `Scripts/` 加入子进程的 `PATH`。创建失败会把错误记到 `self._venv_error`，不中断启动。

6. **生成策略文件**
   在 `policies/` 下创建默认的 `workspace.md`、`git.md`、`security.md`，作为长期规则文本，可被 Agent 读取。采用"文件不存在才创建"策略，不覆盖用户后续维护过的规则。

7. **同步内置 skills**
   `_ensure_builtin_skills()` 把随当前应用版本发布的 skill 同步到 `/skills`。同名内置文件会更新，用户自己新建的其他 skill 目录不会被删除或覆盖。

8. **生成 Git AskPass 脚本**
   在 `secrets/` 下创建当前平台的 `github_askpass.sh` 或 `github_askpass.cmd`。Git 会通过 `core.askPass` 调用脚本，从环境变量获取用户名和 Token。

9. **写入工作区标记文件**
   生成 `.ai_coding_workspace.json`，记录后端类型、平台、根目录、更新时间和虚拟目录列表，方便外部工具识别。

10. **运行期开关与标识**
   - `read_only`：构造参数，开启后只允许只读命令和部分只读 Git 子命令，所有写入接口（`write/edit/upload/run`）直接拒绝。
   - `command_guard_enabled`：由 `LOCAL_SHELL_ENABLE_COMMAND_GUARD` 控制（默认开启，兼容旧变量名 `LOCAL_SHELL_COMMAND_GUARD_ENABLED`）。对 `execute()` 而言，关闭后会跳过 `_deny_reason()` 和 `normalize_safe_command()` 两层校验；兼容接口 `run()` 的行为不同，关闭后只跳过 `_deny_reason()`，仍会执行 `normalize_safe_command()`。
   - `output_encoding`：由 `LOCAL_SHELL_OUTPUT_ENCODING` 配置；macOS 默认 UTF-8，Windows 默认跟随系统代码页，用于子进程输出解码。
   - `id` 属性：返回 `local-shell-<root 的 sha256 前 16 位>`，作为不暴露真实路径的稳定 Sandbox 标识。

---

## 3. 核心方法：命令执行（`execute`）

`execute(command, timeout)` 是 Agent 最常用的入口，执行流程如下：

1. **只读模式拦截**
   若 `read_only=True` 且命令不在 `_read_only_command_allowed()` 的允许列表内（查看类命令 + `git clone/diff/fetch/log/ls-files/pull/rev-parse/show/status` 等准备类子命令，`branch`/`remote` 仅限只读形式），直接返回退出码 126。

2. **安全守卫（第一层）**
   在命令改写之前，先调用 `_deny_reason(command)` 做粗粒度拦截（危险模式、`..` 穿越、工作区外绝对路径），命中则返回退出码 126；
   再调用 `normalize_safe_command(command)` 做命令白名单与 shell 操作符收敛，校验失败同样返回 126。
   这两层都受 `command_guard_enabled` 开关控制，关闭后整段跳过。

3. **命令预处理（`_prepare_command`）**
   - **虚拟路径替换**：用正则匹配 `/projects/xxx`、`/skills/xxx` 等，将其映射为真实路径（并加引号防空格）。
   - **Git AskPass 注入**：如果命令以 `git` 开头，自动添加 `-c credential.helper= -c core.askPass="..."`，强制使用自定义认证脚本。

4. **执行子进程**
   通过 `subprocess.run`，指定 `cwd=self.projects_dir`（默认在项目目录下执行），使用 `shell=True`，并传入包含认证信息和虚拟环境路径的环境变量（由 `_execution_env()` 构建）。超时默认为 3600 秒。

5. **结果封装**
   合并 stdout 和 stderr（用 `<stderr>` 标签分隔），并调用 `_mask_token()` 隐藏 GitHub Token，最终返回 `ExecuteResponse(output, exit_code, truncated)`。超时返回退出码 124，其他异常返回退出码 1，都不向外抛出异常。

---

## 4. 核心方法：文件操作（DeepAgents 协议）

所有文件方法都基于**虚拟路径**，内部通过 `_resolve_virtual_path()` 转换为真实路径。该方法的基础边界是“解析后仍必须位于 `self.root` 内”；写入操作还会额外执行 `_write_deny_reason()` 目录权限检查。

| 方法 | 功能 | 关键逻辑 |
|------|------|----------|
| `ls(path)` | 列出目录内容 | 解析路径，返回包含 `path`、`is_dir`、`size`、`modified_at` 的列表 |
| `read(file_path, offset, limit)` | 读取文件 | 优先 UTF-8 解码，失败回退 Latin-1；支持偏移和行数限制 |
| `write(file_path, content)` | 创建新文件 | 若文件已存在则报错；写入前检查 `_write_deny_reason` |
| `edit(file_path, old_string, new_string, replace_all)` | 替换文件内容 | 类似 `sed`，要求精确匹配；多次出现时必须设置 `replace_all=True` |
| `glob(pattern, path)` | 递归通配符搜索 | 基于 `fnmatch`，返回匹配项的元信息 |
| `grep(pattern, path, glob)` | 内容搜索 | 在文件行中查找子串，返回路径、行号和内容 |
| `upload_files(files)` | 批量上传二进制文件 | 直接写字节，支持错误信息（权限、目录等） |
| `download_files(paths)` | 批量下载 | 返回二进制内容，支持错误状态 |

---

## 5. 安全控制机制

后端在多个层面实施安全策略：

### 5.1 路径边界检查（`_is_under_root`）
文件工具的虚拟路径解析走 `_resolve_virtual_path()`：它先把路径拼到 `self.root` 下并 `Path.resolve()`，再调用 `_is_under_root()` 验证仍在 `self.root` 下，否则抛 `PermissionError`。`_is_under_root()` 本身用 `Path.relative_to()` 判断归属。命令文本里的虚拟路径替换（`_virtual_command_path_replacement`）也走同样的越界校验。

### 5.2 写入目录限制（`_write_deny_reason`）
当前实现采用**写入黑名单**，明确禁止修改以下目录：
- `/policies`（策略文件，只读）
- `/skills`（技能文件，只读）
- `/runtimes`（运行时环境，只读）
- `/logs`（日志目录，只读）
- `/secrets`（敏感凭证，保护）

需要注意，代码并不是通过白名单“只允许 `/projects`、`/tmp`、`/reviews`”。除上述禁止目录外，工作区根目录及其他未列入黑名单的目录也能被文件写入接口修改。`/projects`、`/tmp`、`/reviews` 只是设计上的主要可写区域，不是代码强制的完整白名单。

### 5.3 读取边界与 `secrets` 风险
`_write_deny_reason()` 只控制写入，不控制读取。当前 `_resolve_virtual_path()` 只检查路径是否位于工作区内，因此 `/secrets`、`/policies`、`/skills` 等目录可以通过 `read()`、`ls()` 或 `download_files()` 访问。所以代码中所谓的 `secrets` “保护”目前准确含义是**禁止文件接口写入**，而不是禁止读取。若要存放真实敏感文件，还应增加读取黑名单或改为可读、可写白名单。

### 5.4 命令黑名单（`_deny_reason`）
检查命令中是否包含：
- 路径穿越（`../`）
- 危险系统命令（`format`、`shutdown` 等）
- 工作区外的 POSIX 或 Windows 盘符绝对路径（虚拟路径白名单除外）

这一层是粗粒度拦截，后续还有 `normalize_safe_command`（在外部模块）做更细粒度的白名单归一化。在 `execute()` 中，两层校验都受 `command_guard_enabled` 开关控制；在 `run()` 中，开关只控制 `_deny_reason()`，`normalize_safe_command()` 仍会执行。

---

## 6. macOS / Windows 路径与命令设计

### 6.1 路径映射
- 虚拟路径以 `/` 开头，如 `/projects/my-repo`，后端映射到 `self.root/projects/my-repo`。
- 命令中的虚拟路径会被正则替换为带引号的真实路径，防止空格或特殊字符引起解析错误。

### 6.2 命令预处理
`_prepare_command` 将虚拟路径转换为真实路径，并为 Git 命令注入 AskPass 配置。

### 6.3 Git AskPass
- macOS 使用 `.sh` 脚本并赋予执行权限，Windows 使用 `.cmd` 脚本。
- 环境变量 `GIT_ASKPASS` 指向相应脚本，`GITHUB_ASKPASS_USERNAME` 固定为 `x-access-token`，`GITHUB_ASKPASS_TOKEN` 从环境变量读取。

### 6.4 Python 虚拟环境路径
- macOS 的虚拟环境可执行目录为 `bin/`，Python 文件名为 `python`；Windows 对应 `Scripts/` 和 `python.exe`。

---

## 7. 辅助方法与兼容接口

- **`_execution_env()`**：构建子进程环境，将 `PATH` 指向当前平台的虚拟环境可执行目录，设置 `VIRTUAL_ENV`，注入 `GIT_TERMINAL_PROMPT=0` 以及 AskPass 所需的环境变量（`GIT_ASKPASS`、`GIT_ASKPASS_REQUIRE=force`、`GITHUB_ASKPASS_USERNAME=x-access-token`、`GITHUB_ASKPASS_TOKEN`）。
- **`_mask_token(text)`**：将 `GITHUB_TOKEN`、`GH_TOKEN` 或 `SCM_GITHUB_TOKEN` 替换为 `***`，防止敏感信息泄露到日志或输出。
- **`CommandResult`**：`run()` 返回的数据类，包含 `command/stdout/stderr/exit_code/cwd`，与 `ExecuteResponse` 区分，面向项目历史工具。
- **旧兼容接口**：`run()`、`read_file()`、`write_file()`、`list_files()` 保留给历史工具使用，它们复用新后端的路径解析、写入限制、命令预处理和认证环境，但并非全部通过 DeepAgents 协议方法间接实现。
  - `read_file()` 会调用协议方法 `read()`。
  - `write_file()` 会直接使用 `Path.write_text()` 新建或覆盖文件，但仍经过 `_resolve_virtual_path()` 和 `_write_deny_reason()`。
  - `list_files()` 会直接遍历真实目录，返回相对于 `self.root` 的路径。
  - `run()` 会直接调用 `subprocess.run()`，返回 `CommandResult`，而不是调用 `execute()`。
  - `run()` 通过 `_prepare_run_command()` 准备命令：兼容旧工具常见的 `cd repo && git status` 写法——提取 `cd` 后的目录作为 `subprocess` 的 `cwd`（不把 `cd` 当 shell 片段执行），再对后半段做 `_deny_reason` + `normalize_safe_command` 两层校验和 `_prepare_command` 预处理。
  - `read_file()/write_file()/list_files()` 通过 `_normalize_compat_path()` 把旧工具传入的 `.`、`projects/a.py` 等形式统一成 `/projects/a.py` 虚拟路径，再走 `_resolve_virtual_path()`。

---

## 8. 设计权衡与总结

- **虚拟路径抽象**：使 Agent 无需关心宿主机真实路径，增强可移植性和安全性。
- **双层命令检查**：默认先通过 `_deny_reason()` 拒绝明显危险命令，再通过外部 `normalize_safe_command()` 进行命令白名单和 shell 语法收敛。它们能降低误操作风险，但因为底层仍使用 `shell=True`，且存在可关闭安全守卫的配置，不应将其视为强隔离安全边界。
- **非交互认证**：通过 AskPass 脚本将认证信息隔离在环境变量中，避免 Token 出现在命令历史或日志中。
- **宽严相济的编码处理**：文件读取时 UTF-8 失败自动降级 Latin-1，保证 Agent 能查看任何文件内容，不会因编码错误中断任务。

整体上，这个后端是一个**面向 AI Agent 的轻量级沙箱**，在本地开发场景下提供了足够的安全性和便利性，但文档也强调它并非企业级强隔离，生产环境还需叠加容器、用户隔离等更严格的措施。
