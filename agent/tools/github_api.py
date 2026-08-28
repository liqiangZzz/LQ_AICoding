"""GitHub API 访问封装。

本模块负责读取访问令牌、创建 Pull Request、发布 PR 评论，以及在重复创建时
查找并复用已有 PR。上层 LangChain/DeepAgents 工具定义在 `github_tools.py` 中。
这样的分层可以让 API 调用逻辑脱离模型工具协议，便于单元测试、复用和错误处理。
"""
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from agent.env_utils import get_env


@dataclass(frozen=True)
class GitHubRepo:
    """标准化后的 GitHub 仓库信息。

    owner 和 repo 用于调用 GitHub API；
    clone_url 用于 Git clone/push 等本地命令。
    使用 frozen dataclass 可以避免解析后的仓库信息在调用链中被意外修改。
    """
    owner: str
    repo: str
    clone_url: str


def parse_github_repo_url(repo_url: str) -> GitHubRepo:
    """
    解析 GitHub 仓库地址，返回标准化的仓库信息。

    Args:
        repo_url: 用户输入或任务配置中的 GitHub 仓库地址，支持带 `.git` 后缀的 HTTPS 地址。

    Returns:
        标准化后的 `GitHubRepo`。

    Raises:
        ValueError: URL 不是 github.com 域名，或路径中无法解析出 owner/repo。
    """

    # 去除 URL 前后的空格，并解析出 hostname 和路径
    parsed = urlparse(repo_url.strip())
    hostname = (parsed.hostname or "").lower()

    if hostname not in ("github.com", "www.github.com"):
        raise ValueError("当前仅支持 github.com 仓库地址")

    # 去除路径前后的空格，并按 / 切分，至少需要两个部分：owner 和 repo
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"无法解析 GitHub 仓库地址: {repo_url}")

    # owner 是路径的第一个部分，repo 是第二个部分
    owner = parts[0]
    # 统一去掉 `.git` 后缀，API 路径使用纯 repo 名，clone_url 再补回标准后缀。
    repo = re.sub(r"\.git$", "", parts[1])
    return GitHubRepo(owner=owner, repo=repo, clone_url=f"https://github.com/{owner}/{repo}.git")


def get_github_token() -> str:
    """读取 GitHub 访问令牌。"""
    token = (
            get_env("GITHUB_TOKEN").strip()
            or get_env("GH_TOKEN").strip()
            or get_env("SCM_GITHUB_TOKEN").strip()
    )
    if not token:
        raise RuntimeError(
            "Missing required environment variable: GITHUB_TOKEN, GH_TOKEN or SCM_GITHUB_TOKEN"
        )
    return token


def mask_token(text: str) -> str:
    """
    对文本中的 GitHub Token 做脱敏。

    API 错误、Git 输出和异常信息可能包含访问令牌
    所有写日志或返回给模型的外部错误文本都应该经过该函数做脱敏处理

    Args:
        text: 待脱敏的文本内容。
    Returns:
        脱敏后的文本内容。
    """
    masked = text
    for token_name in ("GITHUB_TOKEN", "GH_TOKEN", "SCM_GITHUB_TOKEN"):
        token = get_env(token_name).strip()
        if token:
            masked = masked.replace(token, "***")
    return masked


def _headers(token: str) -> dict[str, str]:
    """构造 GitHub REST API 所需请求头。"""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_existing_pull_request(
        client: httpx.Client,
        *,
        api_base: str,
        headers: dict[str, str],
        owner: str,
        repo: str,
        head: str,
        base: str,
) -> dict[str, Any] | None:
    """查询相同源分支和目标分支的现有开放 PR。

    Args:
        client: httpx.Client
        api_base: GitHub API 基础 URL
        headers: 请求头
        owner: 仓库所有者
        repo: 仓库名
        head: 源分支
        base: 目标分支
    Returns:
        GitHub API 返回的 PR JSON；如果未找到则返回 None
    """

    # 构造源分支，格式为 owner:branch
    qualified_head = head if ":" in head else f"{owner}:{head}"

    #  调用 GitHub API 查询现有 PR
    response = client.get(
        f"{api_base}/repos/{owner}/{repo}/pulls",
        headers=headers,
        params={"state": "open", "head": qualified_head, "base": base},
    )
    if response.status_code >= 400:
        return None
    pulls = response.json()
    if not pulls:
        return None
    existing = dict(pulls[0])
    existing["reused"] = True
    existing["message"] = "已复用相同源分支和目标分支的现有 Pull Request"
    return existing


def create_pull_request(
        *,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
) -> dict:
    """
    调用 GitHub API 创建 Pull Request。

    Args:
        owner: GitHub 仓库所有者。
        repo: GitHub 仓库名。
        head: 源分支。
        base: 目标分支，通常为 main。
        title: Pull Request 标题。
        body: Pull Request 描述。

    Returns:
        GitHub API 返回的 PR JSON；如果 PR 已存在，则返回带 `reused=True` 的结构。

    Raises:
        RuntimeError: API 返回失败且无法识别为可复用 PR。
    """

    api_base = get_env("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")
    token = get_github_token()
    url = f"{api_base}/repos/{owner}/{repo}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }
    # 创建 Pull Request
    with httpx.Client(timeout=30) as client:
        headers = _headers(token)
        response = client.post(url, headers=headers, json=payload)
        if response.status_code == 422:
            existing = _find_existing_pull_request(
                client,
                api_base=api_base,
                headers=headers,
                owner=owner,
                repo=repo,
                head=head,
                base=base,
            )
            if existing is not None:
                return existing

    if response.status_code >= 400:
        raise RuntimeError(
            f"GitHub 创建 PR 失败: {response.status_code} {mask_token(response.text)}"
        )
    return response.json()


def _github_get(path: str, *, params: dict[str, Any] | None = None) -> dict | list:
    """执行 GitHub GET 请求。

    令牌只放入 Authorization 请求头，避免出现在 URL、代理日志或异常信息中。
    """
    api_base = get_env("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")
    url = f"{api_base}{path}"
    with httpx.Client(timeout=30) as client:
        response = client.get(
            url,
            headers=_headers(get_github_token()),
            params=dict(params or {}),
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"GitHub API 读取失败: {response.status_code} {mask_token(response.text)}"
        )
    return response.json()


def _github_get_all(path: str, *, params: dict[str, Any] | None = None) -> list[Any]:
    """读取 GitHub 分页列表，直到最后一页。"""

    page = 1
    items: list[Any] = []
    while True:
        page_params = {**(params or {}), "per_page": 100, "page": page}
        data = _github_get(path, params=page_params)
        if not isinstance(data, list):
            raise TypeError(f"GitHub API 返回格式异常，期望列表: {path}")
        items.extend(data)
        if len(data) < 100:
            return items
        page += 1

def get_pull_request(*, owner: str, repo: str, number: int) -> dict:
    """读取 GitHub Pull Request 详情。"""

    data = _github_get(f"/repos/{owner}/{repo}/pulls/{number}")
    return data if isinstance(data, dict) else {"items": data}

def list_pull_request_commits(*, owner: str, repo: str, number: int) -> list[Any]:
    """读取 GitHub Pull Request 提交列表。"""
    return _github_get_all(f"/repos/{owner}/{repo}/pulls/{number}/commits")

def list_pull_request_files(*, owner: str, repo: str, number: int) -> list[Any]:
    """读取 GitHub Pull Request 的文件变更列表。"""
    return _github_get_all(f"/repos/{owner}/{repo}/pulls/{number}/files")

def list_pull_request_comments(*, owner: str, repo: str, number: int) -> list[Any]:
    """读取 GitHub Pull Request 的普通评论列表。"""

    # PR 的普通会话评论属于 Issues comments API；pulls/.../comments 是行级审查评论。
    return _github_get_all(f"/repos/{owner}/{repo}/issues/{number}/comments")


def list_pull_request_review_comments(*, owner: str, repo: str, number: int) -> list[Any]:
    """读取 GitHub Pull Request 的行级审查评论列表。"""

    return _github_get_all(f"/repos/{owner}/{repo}/pulls/{number}/comments")

def post_pr_comment(*, owner: str, repo: str, number: int, body: str) -> dict:
    """
    调用 GitHub API 向 Pull Request 发布普通评论。

    Args:
        owner: GitHub 仓库所有者。
        repo: GitHub 仓库名。
        number: Pull Request 编号。
        body: 评论内容。

    Returns:
        GitHub API 返回的评论 JSON。

    Raises:
        RuntimeError: API 返回失败状态码。
    """
    api_base = get_env("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")
    token = get_github_token()
    # GitHub 的 PR 普通评论复用 Issues comments API。
    url = f"{api_base}/repos/{owner}/{repo}/issues/{number}/comments"
    with httpx.Client(timeout=30) as client:
        response = client.post(url, headers=_headers(token), json={"body": body})

    if response.status_code >= 400:
        raise RuntimeError(
            f"GitHub 发布 PR 评论失败: {response.status_code} {mask_token(response.text)}"
        )
    return response.json()
