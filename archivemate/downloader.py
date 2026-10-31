"""从 GitHub 下载归档文件（REST + 流式写盘）。

和推送那边同一个思路：不走 ``git clone``，避免为了拿一个文件去下载整个
仓库。列目录用 tree 接口（一次请求拿到全部路径和体积），取文件内容用
``contents`` 接口的 raw 响应，64 KiB 一块边收边写，内存占用与文件大小无关。

下载过程中可以随时 :meth:`GitHubDownloader.cancel`；取消后已写了一半的
``.part`` 会被清掉，不会留下半个文件。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .errors import DownloadCancelled, DownloadError, GitHubError
from .fsutil import human_size, unique_path
from .github_api import GitHubClient

#: 每次从套接字读多少字节。太小会让循环很密，太大占内存。
CHUNK_SIZE = 64 * 1024

#: 下载时单个请求的空闲超时（秒）。慢网络下给得松一点，真正防卡死靠
#: 「多久没有新数据就报错」这条：连续 STALL_SECONDS 没收到字节就中止。
SOCKET_TIMEOUT = 120.0
STALL_SECONDS = 120.0


@dataclass
class RemoteFile:
    """仓库里的一个文件。"""

    path: str
    size: int = 0
    sha: str = ""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    def describe_size(self) -> str:
        return human_size(self.size) if self.size else "大小未知"


ProgressHandler = Callable[[str, int, int], None]


class GitHubDownloader:
    """把仓库里的文件流式下载到本地目录。

    参数与 :class:`archivemate.github_api.GitHubClient` 对齐；测试可以注入
    一个假的 ``client``，它只需要有 ``_request``（JSON）和 ``open_download``
    （原始字节流）两个方法。
    """

    def __init__(self, token: str = "", owner: str = "", name: str = "",
                 branch: str = "main", *, proxy: str = "",
                 timeout: float = SOCKET_TIMEOUT, logger: Callable[[str], None] | None = None,
                 on_progress: ProgressHandler | None = None,
                 client=None,
                 stream_opener: Callable[[str], tuple] | None = None) -> None:
        self.owner = (owner or "").strip()
        self.name = (name or "").strip()
        self.branch = (branch or "").strip() or "main"
        self._log = logger or (lambda _m: None)
        self._on_progress = on_progress
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._client = client or GitHubClient(token, proxy=proxy, timeout=timeout)
        self._stream_opener = stream_opener or self._default_stream_opener

    # ------------------------------------------------------------------
    # 基本状态
    # ------------------------------------------------------------------

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self.owner}/{self.name}{suffix}"

    def cancel(self) -> None:
        """请求中止下载；正在阻塞的 read 要等这一块收完才会返回。"""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def reset(self) -> None:
        """清掉取消标记，供「再来一次」复用同一个实例。"""
        self._cancel.clear()

    # ------------------------------------------------------------------
    # 列目录
    # ------------------------------------------------------------------

    def list_files(self) -> list[RemoteFile]:
        """列出分支上的全部文件（按路径排序）。空仓库返回空列表。"""
        if not (self.owner and self.name):
            raise DownloadError("还没有配置 GitHub 仓库，请先在设置里填写用户名与仓库名。")
        try:
            ref = self._client._request(
                "GET", self._repo_path(f"/git/ref/heads/{self.branch}"),
                timeout=min(SOCKET_TIMEOUT, 60))
            sha = ((ref or {}).get("object") or {}).get("sha", "")
            if not sha:
                return []
            commit = self._client._request(
                "GET", self._repo_path(f"/git/commits/{sha}"),
                timeout=min(SOCKET_TIMEOUT, 60))
            tree_sha = ((commit or {}).get("tree") or {}).get("sha", "")
            if not tree_sha:
                return []
            tree = self._client._request(
                "GET", self._repo_path(f"/git/trees/{tree_sha}"),
                params={"recursive": "1"}, timeout=min(SOCKET_TIMEOUT, 120))
        except GitHubError as exc:
            if exc.status == 404:
                self._log("远端仓库或分支还不存在。")
                return []
            raise DownloadError(str(exc)) from exc
        except OSError as exc:
            raise DownloadError(f"读取仓库文件列表失败：{exc}") from exc

        files = [
            RemoteFile(path=entry["path"], size=int(entry.get("size") or 0),
                       sha=entry.get("sha", ""))
            for entry in (tree or {}).get("tree", [])
            if entry.get("type") == "blob" and entry.get("path")
        ]
        files.sort(key=lambda item: item.path)
        return files

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def _default_stream_opener(self, path: str):
        return self._client.open_download(
            path, owner=self.owner, name=self.name, ref=self.branch,
            timeout=SOCKET_TIMEOUT)

    def download(self, remote: RemoteFile, dest_dir: Path) -> Path:
        """下载单个文件到 dest_dir，返回实际写入的路径。"""
        if self.cancelled:
            raise DownloadCancelled("已取消下载。")
        dest_dir = Path(dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DownloadError(f"无法创建下载目录 {dest_dir}：{exc}") from exc

        target = unique_path(dest_dir, remote.name)
        partial = target.with_name(target.name + ".part")
        written = 0
        started = time.monotonic()
        last_data = started

        try:
            response, total = self._stream_opener(remote.path)
        except GitHubError as exc:
            raise DownloadError(str(exc)) from exc
        except OSError as exc:
            raise DownloadError(f"打开下载连接失败：{exc}") from exc

        try:
            with response:
                with partial.open("wb") as handle:
                    while True:
                        if self.cancelled:
                            raise DownloadCancelled("已取消下载。")
                        if time.monotonic() - last_data > STALL_SECONDS:
                            raise DownloadError("下载停滞超过 120 秒，已中止。")
                        try:
                            chunk = response.read(CHUNK_SIZE)
                        except OSError as exc:
                            raise DownloadError(f"下载中断：{exc}") from exc
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                        last_data = time.monotonic()
                        self._emit_progress(remote.path, written, total or remote.size)
            if self.cancelled:
                raise DownloadCancelled("已取消下载。")
            os.replace(partial, target)
        except DownloadCancelled:
            self._discard(partial)
            raise
        except DownloadError:
            self._discard(partial)
            raise
        except OSError as exc:
            self._discard(partial)
            raise DownloadError(f"写入 {target.name} 失败：{exc}") from exc

        elapsed = max(0.001, time.monotonic() - started)
        self._log(
            f"已下载 {remote.path}（{human_size(written)}，"
            f"{human_size(int(written / elapsed))}/s）")
        return target

    def download_many(self, files: Sequence[RemoteFile],
                      dest_dir: Path) -> list[Path]:
        """按顺序下载多个文件，任何一个失败都会抛 DownloadError。"""
        saved: list[Path] = []
        for item in files:
            if self.cancelled:
                raise DownloadCancelled("已取消下载。")
            saved.append(self.download(item, dest_dir))
        return saved

    # ------------------------------------------------------------------

    def _emit_progress(self, path: str, done: int, total: int) -> None:
        handler = self._on_progress
        if handler is None:
            return
        try:
            handler(path, done, total)
        except Exception:      # 回调是界面代码，不能让它污染下载循环
            pass

    @staticmethod
    def _discard(partial: Path) -> None:
        try:
            partial.unlink()
        except OSError:
            pass


def select_latest(files: Iterable[RemoteFile]) -> list[RemoteFile]:
    """「一键下载最新」= 全部文件。留着这个名字是为了界面文案好读。"""
    return list(files)
