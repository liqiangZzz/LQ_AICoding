from agent.core.settings import STORE_DB_PATH
from agent.store.sqlite_store import LocalSqliteStore

_local_store: LocalSqliteStore | None = None


def get_local_store() -> LocalSqliteStore:
    """返回进程内共享的业务 SQLite Store。"""

    global _local_store
    if _local_store is None:
        _local_store = LocalSqliteStore(STORE_DB_PATH)
    return _local_store


__all__ = ["LocalSqliteStore", "get_local_store"]
