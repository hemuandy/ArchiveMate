"""通过 GitHub REST API 直接建提交，完全不下载仓库里已有的文件。

为什么不用 ``git push``
-----------------------
``git`` 要在本地有一份**完整**的仓库才能算差量和做推送前的连通性检查：
一次 ``fetch`` 就要把仓库总体积（用户这里已经攒到 49 MB）拉下来。
实测这类国内线路到 GitHub 的大流量下行只有 20 KB/s 上下，
于是「点同步 → 卡住 → 一直同步中」几乎必然发生，而且仓库只会越用越大。

本模块改走 Git Data API：只上传**这次要新增的文件**，其余对象服务器本来就有。
一次同步的流量因此从「仓库总体积」降到「新增文件体积」，与仓库历史无关。

接口与 :class:`archivemate.git_backend.GitBackend` 的同步相关部分保持一致，
``SyncEngine`` 不需要知道用的是哪一个。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .errors import GitHubError, GitError, PushRejected
from .github_api import GitHubClient
from .git_backend import FileToCommit

#: 单次上传体积上限。GitHub 的 blob 接口要求把内容 base64 编码后放进 JSON，
#: 编码会膨胀 33%，所以留足余量；超过这个体积的文件交给 git 命令行后端处理。
MAX_INLINE_BYTES = 45 * 1024 * 1024

_NOOP_PROGRESS = lambda _stage, _message: None  # noqa: E731


class ApiGitBackend:
    """用 GitHub REST API 提交与推送。"""

    # SyncEngine 里会用到的兼容属性
    ssl_backend = ""
    ssl_ca_info = ""

    def __init__(
        self,
        token: str,
        owner: str,
        name: str,
        branch: str = "main",
        *,
        proxy: str = "",
        network_timeout: int = 1800,
        logger: Callable[[str], None] | None = None,
        on_progress: Callable[[str, str], None] | None = None,
        client: GitHubClient | None = None,
    ) -> None:
        self.token = token
        self.owner = owner
        self.name = name
        self.branch = branch
        self.network_timeout = max(30, int(network_timeout))
        self._log = logger or (lambda _message: None)
        self._progress = on_progress or _NOOP_PROGRESS
        self._client = client or GitHubClient(
            token, proxy=proxy, timeout=min(self.network_timeout, 120))
        self._cancelled = False
        self._base_commit: str | None = None
        self._base_tree: str | None = None

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self) -> bool:
        """标记取消；正在进行的单个 HTTP 请求无法打断，但不会再开始下一个。"""
        self._cancelled = True
        return False

    def reset_cancel(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise GitError("同步已取消。")

    # ------------------------------------------------------------------
    # 仓库状态
    # ------------------------------------------------------------------

    @property
    def base_commit(self) -> str | None:
        return self._base_commit

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self.owner}/{self.name}{suffix}"

    def fetch(self) -> None:
        """读出远端分支的最新提交与根树（两个很小的请求）。"""
        self._base_commit = None
        self._base_tree = None
        try:
            ref = self._client._request(
                "GET", self._repo_path(f"/git/ref/heads/{self.branch}"),
                timeout=min(self.network_timeout, 120))
        except GitHubError as exc:
            if exc.status == 404:
                # 分支还不存在（空仓库），下一个提交就是首个提交
                self._log("远端还没有这个分支，将创建首个提交。")
                return
            raise self._as_git_error(exc) from exc

        sha = ((ref or {}).get("object") or {}).get("sha", "")
        if not sha:
            self._log("远端还没有这个分支，将创建首个提交。")
            return

        commit = self._client._request(
            "GET", self._repo_path(f"/git/commits/{sha}"),
            timeout=min(self.network_timeout, 120))
        self._base_commit = sha
        self._base_tree = ((commit or {}).get("tree") or {}).get("sha") or None
        self._log(f"已同步远端 {self.branch} @ {sha[:8]}")

    def existing_paths(self) -> set[str]:
        """基线提交里已有的文件路径（用于避开重名覆盖）。"""
        if not self._base_tree:
            return set()
        try:
            data = self._client._request(
                "GET", self._repo_path(f"/git/trees/{self._base_tree}"),
                params={"recursive": "1"},
                timeout=min(self.network_timeout, 120))
        except GitHubError as exc:
            self._log(f"读取仓库文件列表失败：{exc}")
            return set()
        return {
            entry["path"]
            for entry in (data or {}).get("tree", [])
            if entry.get("type") == "blob" and entry.get("path")
        }

    # ------------------------------------------------------------------
    # 提交与推送
    # ------------------------------------------------------------------

    @staticmethod
    def _as_git_error(exc: GitHubError) -> GitError:
        return GitError(str(exc))

    def _upload_blob(self, item: FileToCommit) -> str:
        source = item.source
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise GitError(f"无法读取 {source.name}：{exc}") from exc
        if size > MAX_INLINE_BYTES:
            raise GitError(
                f"{source.name} 有 {size / 1024 / 1024:.0f} MB，"
                "超过在线提交接口能承受的体积。请先压缩，或把这个文件手动放进归档仓库。")
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise GitError(f"无法读取 {source.name}：{exc}") from exc
        payload = {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        }
        data = self._client._request(
            "POST", self._repo_path("/git/blobs"), payload,
            timeout=self.network_timeout)
        sha = (data or {}).get("sha", "")
        if not sha:
            raise GitError(f"上传 {source.name} 失败：GitHub 没有返回对象编号。")
        return sha

    def build_commit(self, files: Sequence[FileToCommit], message: str) -> str | None:
        """上传文件并用 Git Data API 生成一个提交（不推送）。"""
        items = [item for item in files if item.source.is_file()]
        if not items:
            return None

        entries = []
        for index, item in enumerate(items, start=1):
            self._check_cancelled()
            size = item.source.stat().st_size
            self._progress(
                "push",
                f"[{index}/{len(items)}] 正在上传 {item.source.name}"
                f"（{size / 1024 / 1024:.1f} MB）……")
            sha = self._upload_blob(item)
            entries.append({
                "path": item.dest_rel.replace("\\", "/"),
                "mode": "100644",
                "type": "blob",
                "sha": sha,
            })

        tree_payload: dict = {"tree": entries}
        if self._base_tree:
            tree_payload["base_tree"] = self._base_tree
        tree = self._client._request(
            "POST", self._repo_path("/git/trees"), tree_payload,
            timeout=self.network_timeout)
        tree_sha = (tree or {}).get("sha", "")
        if not tree_sha:
            raise GitError("生成目录树失败：GitHub 没有返回对象编号。")

        if self._base_tree and tree_sha == self._base_tree:
            self._log("没有新内容需要提交。")
            return None

        commit_payload: dict = {"message": message, "tree": tree_sha}
        if self._base_commit:
            commit_payload["parents"] = [self._base_commit]
        commit = self._client._request(
            "POST", self._repo_path("/git/commits"), commit_payload,
            timeout=self.network_timeout)
        commit_sha = (commit or {}).get("sha", "")
        if not commit_sha:
            raise GitError("生成提交失败：GitHub 没有返回提交编号。")
        self._log(f"已生成提交 {commit_sha[:8]}：{len(entries)} 个文件")
        return commit_sha

    def _point_branch_at(self, commit_sha: str) -> None:
        try:
            self._client._request(
                "PATCH", self._repo_path(f"/git/refs/heads/{self.branch}"),
                {"sha": commit_sha, "force": False},
                timeout=self.network_timeout)
        except GitHubError as exc:
            # 422/409：远端在本轮期间有了新提交，force=false 拒绝更新
            if exc.status in (409, 422):
                raise PushRejected("远端有新的提交，需要先同步再推送。") from exc
            raise self._as_git_error(exc) from exc

    def push_with_retry(
        self,
        files: Iterable[FileToCommit],
        message: str,
        *,
        attempts: int = 3,
        delay: float = 3.0,
    ) -> str:
        """上传 → 建提交 → 推进分支；远端被抢先时重新读取再试。"""
        items = [item for item in files if item.source.is_file()]
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                self.fetch()
                commit = self.build_commit(items, message)
                if commit is None:
                    return ""
                self._point_branch_at(commit)
                self._log(f"已推送到 {self.branch}")
                return commit
            except PushRejected as exc:
                last_error = exc
                if attempt >= max(1, attempts):
                    break
                self._log(f"远端有更新，{delay:g} 秒后重试（{attempt}/{attempts}）……")
                time.sleep(max(0.0, delay))
            except GitError:
                raise
            except GitHubError as exc:
                raise self._as_git_error(exc) from exc
        raise PushRejected(str(last_error) if last_error else "推送失败")

    # ------------------------------------------------------------------

    def describe_transport(self) -> str:
        return f"GitHub 在线接口（{self.owner}/{self.name}）"
