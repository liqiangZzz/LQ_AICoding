"""远程仓库与本地工作区的映射辅助。

映射优先级和 remote 验证逻辑见同目录 `repo_mapping_说明.md`。
"""

from __future__ import annotations

import configparser
import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from agent.backends.workspace import Workspace
from agent.store import LocalSqliteStore
from agent.tools.github_api import GitHubRepo, parse_github_repo_url


@dataclass(frozen=True)
class RepoMappingResult:
    """仓库和本地目录的解析结果。"""

    repo: GitHubRepo
    project_dir: str
    local_path: str
    source: str
    mapping: dict | None = None
    remote_matched: bool = False


def normalize_github_repo_url(repo_url: str) -> str:
    """
    把带 token、无 .git、www 域名等形式统一成标准 GitHub clone_url

    仓库映射表使用这个标准地址作为唯一业务间，避免同一个仓库因为不同写法生成多条映射。
    """
    return parse_github_repo_url(repo_url).clone_url


def repo_mapping_id(repo_url: str, project_dir: str) -> str:
    """生成稳定映射 Id，便于同一仓库和目录重复 upsert"""

    # 统一目录写法为 POSIX 路径
    normalized_project_dir = project_dir.replace("\\", "/")
    raw = f"{normalize_github_repo_url(repo_url)}::{normalized_project_dir}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _candidate_remote_urls(repo_url: str) -> set[str]:
    """生成用于比较 remote origin 的标准化候选 URL"""
    repo = normalize_github_repo_url(repo_url)
    return {
        repo.clone_url.lower(),
        repo.clone_url.removesuffix(".git").lower(),
        f"git@github.com:{repo.owner}/{repo.repo}.git".lower(),
        f"git@github.com:{repo.owner}/{repo.repo}".lower(),
    }


def _clean_remote_url(remote_url: str) -> str:
    """清理 remote URL 中的 token，保留 owner/repo 语义用于比较。"""
    text = remote_url.strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        path = parsed.path.strip("/")
        return f"https://{parsed.hostname.lower()}/{path}".removesuffix(".git").lower()
    return text.removesuffix(".git").lower()


def _read_origin_remote(repo_path: Path) -> str | None:
    """读取本地 Git 仓库的 origin remote 地址。"""

    config_path = repo_path / ".git" / "config"
    if not config_path.exists():
        return None
    # 读取 config 文件
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    section = "remote origin"
    # 检查 section 是否存在
    if not parser.has_section(section):
        return None
    # 读取 url
    return parser.get(section, "url", fallback=None)


def _is_nested_projects_dir(project_dir: str) -> bool:
    """
    判断仓库目录是否落入错误的 `projects/projects` 嵌套目录。

    正常业务仓库只应该位于 `projects/<repo>` 或 `projects/<custom-name>`。
    `projects/projects/<repo>` 通常来自命令工作目录和相对路径重复拼接。
    """
    normalized = project_dir.replace("\\", "/").strip("/")
    return normalized == "projects/projects" or normalized.startswith("projects/projects/")


def remote_matches_repo(remote_url: str | None, repo_url: str) -> bool:
    """判断本地 remote origin 是否对应目标 GitHub 仓库。"""
    if not remote_url:
        return False

    # 清理 remote URL 中的 token，保留 owner/repo 语义用于比较
    cleaned = _clean_remote_url(remote_url)
    # 生成用于比较的候选 remote URL 集合
    candidates = {_clean_remote_url(remote_url) for remote_url in _candidate_remote_urls(repo_url)}
    # 判断清理后的 remote URL 是否在候选集合中
    return cleaned in candidates


def _save_mapping(
        store: LocalSqliteStore,
        *,
        repo: GitHubRepo,
        project_dir: str,
        local_path: str,
        source: str,
        notes: str | None = None,
        verified: bool = False,
) -> dict:
    """统一写入仓库目录映射。"""
    return store.upsert_repo_mapping(
        mapping_id=repo_mapping_id(repo.clone_url, project_dir),
        repo_url=repo.clone_url,
        repo_owner=repo.owner,
        repo_name=repo.repo,
        project_dir=project_dir.replace("\\", "/"),
        local_path=local_path,
        source=source,
        notes=notes,
        verified=verified,
    )


def discover_repo_mapping(
        *,
        repo_url: str,
        workspace: Workspace,
        store: LocalSqliteStore,
) -> RepoMappingResult:
    """解析 GitHub 仓库对应的本地 projects 目录。

    顺序：
    1. 读取 SQLite 中已启用映射并验证目录。
    2. 按仓库名检查 `projects/仓库名`。
    3. 只扫描 `projects/*` 下一级 Git 仓库，通过 origin remote 匹配。
    4. 都找不到时返回默认 clone 目录 `projects/仓库名`。
    """
    # 映射不能只相信数据库：目录可能被移动，remote 也可能被用户手动修改。
    # 因此每次使用前都重新读取 .git/config 验证 origin。
    repo = parse_github_repo_url(repo_url)

    # 读取已保存的映射
    existing = store.get_repo_mapping(repo.clone_url)

    # 1. 按仓库名检查 `projects/仓库名`。
    if existing:
        # 第一优先级：相信已经保存过的 active 映射，但必须重新验证。
        # 目录可能被用户删除、移动，或者 remote origin 被手动改到另一个仓库。
        # 所以这里不仅看 SQLite，还要读本地 .git/config 做二次确认。
        project_dir = str(existing["project_dir"]).replace("\\", "/")

        #  检查目录是否落入错误的 projects/projects 子目录
        if _is_nested_projects_dir(project_dir):
            project_dir = ""
        if not project_dir:
            existing = None
            local_path = None
            remote = None
        else:
            local_path = workspace.resolve(project_dir)
            remote = _read_origin_remote(local_path)

        if (local_path and local_path.exists()
                and (local_path / ".git").exists()
                and remote_matches_repo(remote,repo.clone_url)):

            store.mark_repo_mapping_verified(str(existing["id"]), notes="映射已通过本地 remote 验证")
            return RepoMappingResult(
                repo=repo,
                project_dir=project_dir,
                local_path=str(local_path),
                source="stored",
                mapping=existing,
                remote_matched=True,
            )

    # 2. 按仓库名检查 `projects/仓库名`。
    default_project_dir = (Path("projects") / repo.repo).as_posix()
    # 都找不到时返回默认 clone 目录 `projects/仓库名`。
    default_path = workspace.resolve(default_project_dir)
    # 读取默认目录的 remote origin
    default_remote = _read_origin_remote(default_path)

    # 检查默认目录是否存在且匹配
    if (default_path.exists() and (default_path / ".git").exists()
            and remote_matches_repo(default_remote,repo.clone_url)):
        # 第二优先级：按仓库名匹配默认目录。
        # 这是最常见的情况：第一次 clone 后通常就是 projects/<repo>。
        # 命中后立即写回映射表，后续任务就可以走 stored 分支。
        mapping = _save_mapping(
            store,
            repo=repo,
            project_dir=default_project_dir,
            local_path=str(default_path),
            source="auto_discovered",
            notes="按仓库名匹配 projects 下默认目录",
            verified=True,
        )
        return RepoMappingResult(
            repo=repo,
            project_dir=default_project_dir,
            local_path=str(default_path),
            source="default_name",
            mapping=mapping,
            remote_matched=True,
        )

    # 3. 只扫描 `projects/*` 下一级 Git 仓库，通过 origin remote 匹配。
    projects_path = workspace.resolve("projects")
    if projects_path.exists():
        # 第三优先级：扫描 projects 下一级目录。
        # 这个分支用于处理用户手动改过目录名的情况，例如 projects/ai_coding_demo。
        # 为了控制成本，只扫描下一层 Git 仓库，不做全盘递归搜索。
        for child in sorted(projects_path.iterdir(), key=lambda item: item.name.lower()):
            if child.name.lower() == "projects":
                continue
            if not child.is_dir() or not (child / ".git").exists():
                continue
            # 读取子目录的 remote origin
            remote = _read_origin_remote(child)
            # 判断 remote origin 是否匹配
            if remote_matches_repo(remote, repo.clone_url):
                project_dir = f"projects/{child.name}"
                mapping = _save_mapping(
                    store,
                    repo=repo,
                    project_dir=project_dir,
                    local_path=str(child),
                    source="auto_discovered",
                    notes="扫描 projects 下 Git remote 后发现",
                    verified=True,
                )
                return RepoMappingResult(
                    repo=repo,
                    project_dir=project_dir,
                    local_path=str(child),
                    source="projects_scan",
                    mapping=mapping,
                    remote_matched=True,
                )

    # 最后仍未找到时，不立即写库，因为此时目录可能还不存在。
    # runtime 会使用这个 default_clone_path 去执行 git clone；clone 成功后再调用
    # save_clone_mapping 持久化，避免把未验证目录提前写成 active 映射。
    return RepoMappingResult(
        repo=repo,
        project_dir=default_project_dir,
        local_path=str(default_path),
        source="default_clone_path",
        mapping=existing,
        remote_matched=False,
    )


def save_clone_mapping(
        *,
        repo_url: str,
        project_dir: str,
        local_path: str,
        store: LocalSqliteStore,
        source: str = "clone_created",
) -> dict:
    """clone 或用户手动指定目录成功后保存映射。"""
    repo = parse_github_repo_url(repo_url)
    return _save_mapping(
        store,
        repo=repo,
        project_dir=project_dir,
        local_path=local_path,
        source=source,
        notes="仓库已准备到本地目录",
        verified=True,
    )
