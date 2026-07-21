from typing_extensions import Literal


TaskKind = Literal["coding", "analysis", "planning", "qa", "sync", "inspect"]



def is_read_only_task(task_kind: TaskKind) -> bool:
    """只读任务不暴露写文件、命令执行、Git 提交和 PR 工具。"""

    return task_kind in {"analysis", "planning", "qa", "inspect"}