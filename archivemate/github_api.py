"""GitHub REST API 客户端（仅标准库 urllib，天然支持代理）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import __version__
from .errors import GitHubError

API_ROOT = "https://api.github.com"
USER_AGENT = f"ArchiveMate/{__version__}"


@dataclass
class RepositoryInfo:
    owner: str
    name: str
    private: bool
    default_branch: str
    clone_url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class UserInfo:
    login: str
    name: str

    @property
    def display(self) -> str:
        return self.name or self.login


class GitHubClient:
    def __init__(self, token: str, *, proxy: str = "", timeout: float = 30.0) -> None:
        self.token = token
        self.proxy = proxy.strip()
        self.timeout = timeout
        handlers: list[urllib.request.BaseHandler] = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({
                "http": self.proxy, "https": self.proxy,
            }))
        else:
            handlers.append(urllib.request.ProxyHandler())
        handlers.append(urllib.request.HTTPSHandler())
        self._opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None,
                 *, params: dict | None = None) -> Any:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise GitHubError(self._explain(exc), status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(
                f"无法连接 GitHub：{exc.reason}。请检查网络或代理设置。") from exc
        except TimeoutError as exc:
            raise GitHubError("连接 GitHub 超时，请检查网络或代理设置。") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub 返回了无法解析的内容。") from exc

    @staticmethod
    def _explain(exc: urllib.error.HTTPError) -> str:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = detail.get("message", "")
        except Exception:
            message = ""
        if exc.code == 401:
            return "Token 无效或已过期，请重新填写。"
        if exc.code == 403:
            return ("Token 权限不足或触发了速率限制。"
                    "请确认 Token 勾选了 repo（私有仓库）权限。")
        if exc.code == 404:
            return f"GitHub 上找不到该资源。{message}".strip()
        if exc.code == 422:
            return f"请求被拒绝：{message or '参数不合法'}"
        return f"GitHub 返回 {exc.code}：{message or exc.reason}"

    # ------------------------------------------------------------------
    # 具体接口
    # ------------------------------------------------------------------

    def get_user(self) -> UserInfo:
        data = self._request("GET", "/user")
        return UserInfo(login=data.get("login", ""), name=data.get("name") or "")

    def list_repositories(self, limit: int = 200) -> list[RepositoryInfo]:
        repos: list[RepositoryInfo] = []
        page = 1
        while len(repos) < limit:
            page_items = self._request(
                "GET", "/user/repos",
                params={"per_page": 100, "page": page,
                        "affiliation": "owner,collaborator",
                        "sort": "updated"},
            )
            if not page_items:
                break
            for item in page_items:
                owner = (item.get("owner") or {}).get("login", "")
                repos.append(RepositoryInfo(
                    owner=owner,
                    name=item.get("name", ""),
                    private=bool(item.get("private")),
                    default_branch=item.get("default_branch") or "main",
                    clone_url=item.get("clone_url") or "",
                ))
            if len(page_items) < 100:
                break
            page += 1
        return repos[:limit]

    def get_repository(self, owner: str, name: str) -> RepositoryInfo:
        data = self._request("GET", f"/repos/{owner}/{name}")
        return RepositoryInfo(
            owner=(data.get("owner") or {}).get("login", owner),
            name=data.get("name", name),
            private=bool(data.get("private")),
            default_branch=data.get("default_branch") or "main",
            clone_url=data.get("clone_url") or "",
        )

    def create_repository(self, name: str, *, private: bool = True,
                          description: str = "") -> RepositoryInfo:
        payload = {
            "name": name,
            "private": private,
            "auto_init": True,
            "description": description or "ArchiveMate 归档仓库",
        }
        data = self._request("POST", "/user/repos", payload)
        return RepositoryInfo(
            owner=(data.get("owner") or {}).get("login", ""),
            name=data.get("name", name),
            private=bool(data.get("private", private)),
            default_branch=data.get("default_branch") or "main",
            clone_url=data.get("clone_url") or "",
        )
