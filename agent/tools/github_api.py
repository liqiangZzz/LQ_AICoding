"""GitHub API 访问封装。

本模块负责读取访问令牌、创建 Pull Request、发布 PR 评论，以及在重复创建时
查找并复用已有 PR。上层 LangChain/DeepAgents 工具定义在 `github_tools.py` 中。
"""
from dataclasses import dataclass
from typing import Any

import httpx

from agent.env_utils import get_env


@dataclass(frozen=True)
class GitHubRepo:
    """标准化后的 GitHub 仓库信息。"""
    owner: str
    repo: str
    clone_url: str


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
    """查询相同源分支和目标分支的现有开放 PR。"""
    qualified_head = head if ":" in head else f"{owner}:{head}"
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
