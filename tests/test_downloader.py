"""从 GitHub 下载的测试：列目录、流式写盘、进度、取消、目录配置。"""

from __future__ import annotations

import io
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from archivemate import downloader
from archivemate.config import Config
from archivemate.downloader import (
    CHUNK_SIZE, GitHubDownloader, RemoteFile, select_latest,
)
from archivemate.errors import DownloadCancelled, DownloadError
from archivemate.github_api import GitHubError

# ======================================================================
# 工具
# ======================================================================

SANDBOX = Path(__file__).resolve().parent / ".sandbox"
SANDBOX.mkdir(parents=True, exist_ok=True)
_COUNTER = 0


def _mkdtemp(prefix: str = "dl-") -> Path:
    """不用 tempfile.mkdtemp：0o700 会在 Windows 上写出受限 DACL。"""
    global _COUNTER
    _COUNTER += 1
    target = SANDBOX / f"{prefix}{os.getpid()}-{_COUNTER}"
    target.mkdir(parents=True, exist_ok=True)
    return target


class _FakeResponse:
    """假的 streaming 响应：按块吐字节，可注入异常或中途取消。"""

    def __init__(self, payload: bytes, *, cancel_after: int | None = None,
                 hook=None, error_after: int | None = None) -> None:
        self._buffer = io.BytesIO(payload)
        self._chunks = 0
        self._cancel_after = cancel_after
        self._hook = hook
        self._error_after = error_after
        self.closed = False

    def read(self, size: int = CHUNK_SIZE) -> bytes:
        self._chunks += 1
        if self._cancel_after is not None and self._chunks > self._cancel_after:
            if self._hook is not None:
                self._hook()
        if self._error_after is not None and self._chunks > self._error_after:
            raise OSError("connection reset")
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False


class _FakeClient:
    """只实现 downloader 真正用到的两个方法。"""

    def __init__(self, tree: dict | None = None, payload: bytes = b"",
                 *, ref_error: int | None = None, response=None) -> None:
        self.tree = tree or {}
        self.payload = payload
        self.ref_error = ref_error
        self.requests: list[tuple[str, str]] = []
        self.response = response

    def _request(self, method: str, path: str, **kwargs):
        self.requests.append((method, path))
        if self.ref_error is not None:
            raise GitHubError("找不到仓库", status=self.ref_error)
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "c0ffee"}}
        if "/git/commits/" in path:
            return {"tree": {"sha": "tree1"}}
        if "/git/trees/" in path:
            return self.tree
        raise AssertionError(f"意外的请求：{path}")

    def open_download(self, path, **_kwargs):
        response = self.response or _FakeResponse(self.payload)
        return response, len(self.payload)


def _tree(*entries) -> dict:
    return {"tree": list(entries)}


def _blob(path: str, size: int = 0) -> dict:
    return {"path": path, "type": "blob", "size": size, "sha": f"sha-{path}"}


# ======================================================================
# 列目录
# ======================================================================


class TestListFiles(unittest.TestCase):
    def test_lists_blobs_sorted_and_skips_trees(self):
        client = _FakeClient(_tree(
            _blob("b/2.pdf", 20), _blob("a/1.pptx", 10),
            {"path": "dir", "type": "tree", "sha": "t"},
        ))
        files = GitHubDownloader(owner="o", name="n", client=client).list_files()
        self.assertEqual([f.path for f in files], ["a/1.pptx", "b/2.pdf"])
        self.assertEqual(files[0].name, "1.pptx")
        self.assertEqual(files[0].folder, "a")
        self.assertEqual(files[1].size, 20)

    def test_uses_three_small_requests(self):
        client = _FakeClient(_tree(_blob("a.pdf")))
        GitHubDownloader(owner="o", name="n", client=client).list_files()
        paths = [path for _method, path in client.requests]
        self.assertEqual(paths, [
            "/repos/o/n/git/ref/heads/main",
            "/repos/o/n/git/commits/c0ffee",
            "/repos/o/n/git/trees/tree1",
        ])

    def test_missing_repo_returns_empty(self):
        client = _FakeClient(ref_error=404)
        self.assertEqual(
            GitHubDownloader(owner="o", name="n", client=client).list_files(), [])

    def test_other_errors_become_download_error(self):
        client = _FakeClient(ref_error=500)
        with self.assertRaises(DownloadError):
            GitHubDownloader(owner="o", name="n", client=client).list_files()

    def test_missing_repository_settings(self):
        with self.assertRaises(DownloadError):
            GitHubDownloader(client=_FakeClient()).list_files()

    def test_select_latest_returns_everything(self):
        files = [RemoteFile("a"), RemoteFile("b")]
        self.assertEqual(select_latest(files), files)


# ======================================================================
# 下载
# ======================================================================


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.root = _mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _downloader(self, payload: bytes, **kwargs) -> GitHubDownloader:
        self.progress: list[tuple[str, int, int]] = []
        self.logs: list[str] = []
        client = _FakeClient(payload=payload, **{
            k: v for k, v in kwargs.items() if k in {"response"}
        })
        return GitHubDownloader(
            owner="o", name="n", client=client,
            logger=self.logs.append,
            on_progress=lambda p, d, t: self.progress.append((p, d, t)))

    def test_writes_file_and_reports_progress(self):
        payload = b"x" * (CHUNK_SIZE * 2 + 7)
        downloader_ = self._downloader(payload)
        target = downloader_.download(RemoteFile("2026/a.pdf", len(payload)), self.root)

        self.assertEqual(target, self.root / "a.pdf")
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(target.with_name("a.pdf.part").exists())
        # 每块一次进度，最后一次等于总字节
        self.assertGreaterEqual(len(self.progress), 3)
        self.assertEqual(self.progress[-1], ("2026/a.pdf", len(payload), len(payload)))
        self.assertTrue(any("/s" in line for line in self.logs))

    def test_creates_destination_directory(self):
        downloader_ = self._downloader(b"abc")
        dest = self.root / "deep" / "deeper"
        target = downloader_.download(RemoteFile("a.pdf"), dest)
        self.assertTrue(target.is_file())

    def test_local_name_collision_gets_suffix(self):
        (self.root / "a.pdf").write_bytes(b"already here")
        downloader_ = self._downloader(b"new content")
        target = downloader_.download(RemoteFile("a.pdf"), self.root)
        self.assertEqual(target.name, "a (2).pdf")
        self.assertEqual((self.root / "a.pdf").read_bytes(), b"already here",
                         "本地已有文件不能被覆盖")

    def test_cancel_before_start(self):
        downloader_ = self._downloader(b"abc")
        downloader_.cancel()
        with self.assertRaises(DownloadCancelled):
            downloader_.download(RemoteFile("a.pdf"), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancel_midway_cleans_partial(self):
        response = _FakeResponse(b"x" * (CHUNK_SIZE * 4), cancel_after=1)
        downloader_ = self._downloader(b"", response=response)
        downloader_.cancel  # noqa: B018 —— 只是确认接口存在
        # 让响应第一块之后自己触发取消
        response._hook = downloader_.cancel
        with self.assertRaises(DownloadCancelled):
            downloader_.download(RemoteFile("a.pdf"), self.root)
        self.assertEqual(list(self.root.iterdir()), [], "不能留下 .part")

    def test_read_error_becomes_download_error(self):
        response = _FakeResponse(b"x" * (CHUNK_SIZE * 4), error_after=1)
        downloader_ = self._downloader(b"", response=response)
        with self.assertRaises(DownloadError):
            downloader_.download(RemoteFile("a.pdf"), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_stall_detection(self):
        downloader_ = self._downloader(b"x" * 1000)
        with mock.patch.object(downloader, "STALL_SECONDS", -1.0):
            with self.assertRaises(DownloadError) as ctx:
                downloader_.download(RemoteFile("a.pdf"), self.root)
        self.assertIn("停滞", str(ctx.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_download_many_keeps_order(self):
        downloader_ = self._downloader(b"payload")
        files = [RemoteFile("b.pdf"), RemoteFile("a.pdf")]
        saved = downloader_.download_many(files, self.root)
        self.assertEqual([p.name for p in saved], ["b.pdf", "a.pdf"])

    def test_download_many_stops_when_cancelled(self):
        downloader_ = self._downloader(b"payload")
        downloader_.cancel()
        with self.assertRaises(DownloadCancelled):
            downloader_.download_many([RemoteFile("a.pdf")], self.root)

    def test_reset_clears_cancel_flag(self):
        downloader_ = self._downloader(b"abc")
        downloader_.cancel()
        self.assertTrue(downloader_.cancelled)
        downloader_.reset()
        self.assertFalse(downloader_.cancelled)
        self.assertTrue(downloader_.download(RemoteFile("a.pdf"), self.root).is_file())

    def test_progress_callback_exception_is_swallowed(self):
        client = _FakeClient(payload=b"abc")

        def boom(_path, _done, _total):
            raise RuntimeError("界面炸了")

        downloader_ = GitHubDownloader(owner="o", name="n", client=client,
                                       on_progress=boom)
        self.assertTrue(downloader_.download(RemoteFile("a.pdf"), self.root).is_file())


class TestRemoteFile(unittest.TestCase):
    def test_name_folder_size(self):
        item = RemoteFile("2026-2027学年/上学期/课件/数学/a.pptx", size=1536)
        self.assertEqual(item.name, "a.pptx")
        self.assertEqual(item.folder, "2026-2027学年/上学期/课件/数学")
        self.assertEqual(item.describe_size(), "1.5 KB")

    def test_top_level_file_has_empty_folder(self):
        self.assertEqual(RemoteFile("a.pdf").folder, "")

    def test_unknown_size(self):
        self.assertEqual(RemoteFile("a.pdf").describe_size(), "大小未知")


# ======================================================================
# 下载目录配置
# ======================================================================


class TestDownloadFolderConfig(unittest.TestCase):
    def test_defaults_to_user_downloads(self):
        cfg = Config.default()
        self.assertEqual(cfg.download_folder, "")
        self.assertEqual(cfg.resolved_download_folder(),
                         Path.home() / "Downloads")

    def test_custom_folder_wins(self):
        cfg = Config.default()
        cfg.download_folder = str(SANDBOX / "接收")
        self.assertEqual(cfg.resolved_download_folder(), SANDBOX / "接收")

    def test_roundtrip_through_json(self):
        cfg = Config.default()
        cfg.download_folder = str(SANDBOX / "d")
        again = Config.from_dict(cfg.to_dict())
        self.assertEqual(again.download_folder, cfg.download_folder)
        self.assertEqual(again.resolved_download_folder(), SANDBOX / "d")

    def test_blank_after_normalize_falls_back(self):
        cfg = Config.from_dict({"download_folder": "   "})
        cfg.normalize()
        self.assertEqual(cfg.resolved_download_folder(),
                         Path.home() / "Downloads")


if __name__ == "__main__":
    unittest.main()
