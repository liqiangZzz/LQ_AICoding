from pathlib import Path

from agent.backends.permissions import assert_path_inside


class Workspace():

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | Path = '.') -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return assert_path_inside(candidate, self.root)
