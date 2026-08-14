from pathlib import Path

from agent.backends.permissions import assert_path_inside


class Workspace:
    """本地工作区封装。

    参数：
        root: 工作区根目录，例如 macOS 的 `~/ai_workspace` 或 Windows 的 `E:\\ai_workspace`。

    设计原则：
    - 初始化时确保 root 目录存在。
    - 所有相对路径都以 root 为基准解析。
    - 所有解析结果都必须通过 `assert_path_inside` 校验。

    这样可以避免模型传入 `..`、绝对路径或奇怪路径时跳出工作区。
    """

    def __init__(self, root: Path):
        """创建工作区对象，并确保根目录存在。"""
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | Path = '.') -> Path:
        """解析路径并确保结果仍在工作区内。

        参数：
            path: 可以是相对路径，也可以是绝对路径。

        返回：
            解析后的绝对路径。

        安全边界：
            如果最终路径不在 `self.root` 内，会抛出 `WorkspacePermissionError`。
            这保证了调用方不能通过 `../` 或外部绝对路径访问工作区外文件。
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return assert_path_inside(candidate, self.root)
