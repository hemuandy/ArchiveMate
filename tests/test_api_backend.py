"""自测：走 GitHub REST 接口的推送后端（不下载仓库已有内容）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivemate import api_backend  # noqa: E402
from archivemate.api_backend import ApiGitBackend  # noqa: E402
from archivemate.config import Config  # noqa: E402
from archivemate.errors import GitHubError, GitError, PushRejected  # noqa: E402
from archivemate.git_backend import FileToCommit  # noqa: E402
from archivemate.sync import SyncEngine  # noqa: E402
from archivemate.tracker import Tracker  # noqa: E402


def _mkdtemp(prefix: str) -> Path:
    """不用 tempfile.mkdtemp：它给的 0o700 会让 Windows 建子目录报拒绝访问。"""
    path = ROOT / f".testrun-{prefix}-{os.getpid()}-{id(prefix)}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


class _FakeClient:
    """按 (方法, 路径) 返回预设响应的假客户端，并记录每次调用。"""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, str, object, dict]] = []

    def _request(self, method: str, path: str, payload=None, **kwargs):
        self.calls.append((method, path, payload, kwargs))
        key = (method, path)
        if key not in self.responses:
            raise AssertionError(f"未预期的请求：{method} {path}")
        answer = self.responses[key]
        if isinstance(answer, list):  # 依次返回，用完后停在最后一个
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer()
        return answer

    def paths(self) -> list[str]:
        return [path for _method, path, _payload, _kwargs in self.calls]


def _backend(client: _FakeClient, **kwargs) -> ApiGitBackend:
    return ApiGitBackend(
        "ghp_faketoken", "owner", "repo", "main", client=client, **kwargs)


class TestFetchAndTree(unittest.TestCase):
    def setUp(self) -> None:
        self.work = _mkdtemp("api")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_fetch_reads_branch_and_root_tree(self) -> None:
        client = _FakeClient({
            ("GET", "/repos/owner/repo/git/ref/heads/main"):
                {"object": {"sha": "aaaa111122223333"}},
            ("GET", "/repos/owner/repo/git/commits/aaaa111122223333"):
                {"tree": {"sha": "tree000111"}},
        })
        logs: list[str] = []
        backend = _backend(client, logger=logs.append)

        backend.fetch()

        self.assertEqual(backend.base_commit, "aaaa111122223333")
        self.assertTrue(any("已同步远端 main @ aaaa1111" in line for line in logs))

    def test_fetch_on_missing_branch_treats_repo_as_empty(self) -> None:
        client = _FakeClient({
            ("GET", "/repos/owner/repo/git/ref/heads/main"):
                GitHubError("没找到", status=404),
        })
        logs: list[str] = []
        backend = _backend(client, logger=logs.append)

        backend.fetch()

        self.assertIsNone(backend.base_commit)
        self.assertTrue(any("首个提交" in line for line in logs))

    def test_fetch_only_touches_two_endpoints(self) -> None:
        """关键约束：一次 fetch 只发两个小请求，绝不下载文件内容。"""
        client = _FakeClient({
            ("GET", "/repos/owner/repo/git/ref/heads/main"):
                {"object": {"sha": "bbb"}},
            ("GET", "/repos/owner/repo/git/commits/bbb"):
                {"tree": {"sha": "ttt"}},
        })
        _backend(client).fetch()

        self.assertEqual(len(client.calls), 2)

    def test_existing_paths_lists_only_blobs(self) -> None:
        client = _FakeClient({
            ("GET", "/repos/owner/repo/git/ref/heads/main"):
                {"object": {"sha": "bbb"}},
            ("GET", "/repos/owner/repo/git/commits/bbb"):
                {"tree": {"sha": "ttt"}},
            ("GET", "/repos/owner/repo/git/trees/ttt"): {
                "tree": [
                    {"path": "2026/10/pdf/a.pdf", "type": "blob"},
                    {"path": "2026", "type": "tree"},
                    {"path": "README.md", "type": "blob"},
                ]
            },
        })
        backend = _backend(client)
        backend.fetch()

        self.assertEqual(backend.existing_paths(), {"2026/10/pdf/a.pdf", "README.md"})
        tree_call = client.calls[-1]
        self.assertEqual(tree_call[3].get("params"), {"recursive": "1"})

    def test_existing_paths_survives_api_error(self) -> None:
        client = _FakeClient({
            ("GET", "/repos/owner/repo/git/ref/heads/main"):
                {"object": {"sha": "bbb"}},
            ("GET", "/repos/owner/repo/git/commits/bbb"):
                {"tree": {"sha": "ttt"}},
            ("GET", "/repos/owner/repo/git/trees/ttt"):
                GitHubError("服务端抽风", status=500),
        })
        backend = _backend(client)
        backend.fetch()

        self.assertEqual(backend.existing_paths(), set())


class TestBuildCommit(unittest.TestCase):
    def setUp(self) -> None:
        self.work = _mkdtemp("apic")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.pdf = self.work / "数学课件.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 hello")

    def _client(self, tree_sha: str = "tree-new", commit_sha: str = "commit-new"):
        return _FakeClient({
            ("POST", "/repos/owner/repo/git/blobs"): {"sha": "blob-1"},
            ("POST", "/repos/owner/repo/git/trees"): {"sha": tree_sha},
            ("POST", "/repos/owner/repo/git/commits"): {"sha": commit_sha},
        })

    def test_uploads_base64_and_keeps_existing_tree(self) -> None:
        client = self._client()
        backend = _backend(client)
        backend._base_tree = "tree-base"
        backend._base_commit = "commit-base"

        sha = backend.build_commit(
            [FileToCommit(self.pdf, "2026/10/pdf/数学课件.pdf")], "add: x")

        self.assertEqual(sha, "commit-new")
        blob_payload = client.calls[0][2]
        self.assertEqual(blob_payload["encoding"], "base64")
        self.assertEqual(base64.b64decode(blob_payload["content"]), self.pdf.read_bytes())

        tree_payload = client.calls[1][2]
        self.assertEqual(tree_payload["base_tree"], "tree-base")
        entry = tree_payload["tree"][0]
        self.assertEqual(entry["path"], "2026/10/pdf/数学课件.pdf")
        self.assertEqual(entry["mode"], "100644")
        self.assertEqual(entry["type"], "blob")

        commit_payload = client.calls[2][2]
        self.assertEqual(commit_payload["tree"], "tree-new")
        self.assertEqual(commit_payload["parents"], ["commit-base"])

    def test_first_commit_has_no_parents(self) -> None:
        client = self._client()
        backend = _backend(client)

        backend.build_commit([FileToCommit(self.pdf, "a.pdf")], "first")

        tree_payload = client.calls[1][2]
        self.assertNotIn("base_tree", tree_payload)
        self.assertNotIn("parents", client.calls[2][2])

    def test_unchanged_tree_returns_no_commit(self) -> None:
        client = self._client(tree_sha="tree-base")
        backend = _backend(client)
        backend._base_tree = "tree-base"

        self.assertIsNone(backend.build_commit([FileToCommit(self.pdf, "a.pdf")], "noop"))
        self.assertEqual([c[1] for c in client.calls],
                         ["/repos/owner/repo/git/blobs", "/repos/owner/repo/git/trees"])

    def test_missing_file_is_ignored(self) -> None:
        backend = _backend(self._client())
        ghost = self.work / "不存在.pdf"

        self.assertIsNone(backend.build_commit([FileToCommit(ghost, "x.pdf")], "m"))

    def test_oversize_file_is_refused_with_advice(self) -> None:
        backend = _backend(self._client())
        with mock.patch.object(api_backend, "MAX_INLINE_BYTES", 10):
            with self.assertRaises(GitError) as caught:
                backend.build_commit([FileToCommit(self.pdf, "a.pdf")], "big")
        self.assertIn("压缩", str(caught.exception))

    def test_cancel_stops_before_next_upload(self) -> None:
        client = self._client()
        backend = _backend(client)
        backend.cancel()

        with self.assertRaises(GitError):
            backend.build_commit([FileToCommit(self.pdf, "a.pdf")], "m")
        self.assertEqual(client.calls, [])

    def test_progress_reports_each_file(self) -> None:
        seen: list[str] = []
        backend = _backend(self._client(), on_progress=lambda _s, m: seen.append(m))

        backend.build_commit([FileToCommit(self.pdf, "a.pdf")], "m")

        self.assertTrue(any("[1/1]" in message and "数学课件.pdf" in message
                            for message in seen))


class TestPushWithRetry(unittest.TestCase):
    def setUp(self) -> None:
        self.work = _mkdtemp("apip")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.pdf = self.work / "a.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 push")

    def _client(self, patch_results):
        return _FakeClient({
            ("GET", "/repos/owner/repo/git/ref/heads/main"):
                {"object": {"sha": "base-commit"}},
            ("GET", "/repos/owner/repo/git/commits/base-commit"):
                {"tree": {"sha": "base-tree"}},
            ("POST", "/repos/owner/repo/git/blobs"): {"sha": "blob-1"},
            ("POST", "/repos/owner/repo/git/trees"): {"sha": "tree-new"},
            ("POST", "/repos/owner/repo/git/commits"): {"sha": "commit-new"},
            ("PATCH", "/repos/owner/repo/git/refs/heads/main"): patch_results,
        })

    def test_pushes_without_force(self) -> None:
        client = self._client([{"object": {"sha": "commit-new"}}])
        backend = _backend(client)

        sha = backend.push_with_retry([FileToCommit(self.pdf, "a.pdf")], "m")

        self.assertEqual(sha, "commit-new")
        patch = [c for c in client.calls if c[0] == "PATCH"][0]
        self.assertEqual(patch[2], {"sha": "commit-new", "force": False})

    def test_huge_file_raises_before_uploading(self) -> None:
        client = self._client([{"object": {}}])
        backend = _backend(client)
        with mock.patch.object(api_backend, "MAX_INLINE_BYTES", 5):
            with self.assertRaises(GitError):
                backend.push_with_retry([FileToCommit(self.pdf, "a.pdf")], "m")
        # 读了远端状态就停下，绝不把文件塞进请求
        self.assertEqual([c[0] for c in client.calls], ["GET", "GET"])

    def test_retries_when_remote_moved_forward(self) -> None:
        client = self._client([
            GitHubError("更新被拒绝", status=422),
            {"object": {"sha": "commit-new"}},
        ])
        backend = _backend(client)
        with mock.patch.object(api_backend.time, "sleep") as sleeper:
            sha = backend.push_with_retry(
                [FileToCommit(self.pdf, "a.pdf")], "m", attempts=2, delay=0.01)

        self.assertEqual(sha, "commit-new")
        sleeper.assert_called_once()
        self.assertEqual(len([c for c in client.calls if c[0] == "PATCH"]), 2)

    def test_gives_up_after_attempts(self) -> None:
        client = self._client([GitHubError("冲突", status=409)])
        backend = _backend(client)
        with mock.patch.object(api_backend.time, "sleep"):
            with self.assertRaises(PushRejected):
                backend.push_with_retry(
                    [FileToCommit(self.pdf, "a.pdf")], "m", attempts=3, delay=0)

        self.assertEqual(len([c for c in client.calls if c[0] == "PATCH"]), 3)

    def test_no_files_means_nothing_to_do(self) -> None:
        client = self._client([{"object": {}}])
        backend = _backend(client)

        self.assertEqual(backend.push_with_retry([], "m"), "")

    def test_other_api_error_becomes_git_error(self) -> None:
        client = self._client([GitHubError("服务器炸了", status=500)])
        backend = _backend(client)

        with self.assertRaises(GitError):
            backend.push_with_retry([FileToCommit(self.pdf, "a.pdf")], "m", attempts=1)


class TestBackendSelection(unittest.TestCase):
    """SyncEngine 应该优先走在线接口，只有超大文件才退回 git 命令行。"""

    def setUp(self) -> None:
        self.root = _mkdtemp("sel")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = Config()
        self.config.repository.owner = "owner"
        self.config.repository.name = "repo"
        self.config.repository.branch = "main"

    def _engine(self, token: str | None) -> SyncEngine:
        engine = SyncEngine(
            self.config, Tracker(self.root / "state.json"),
            token_provider=lambda: token, logger=lambda _m: None)
        return engine

    def test_uses_api_when_token_and_repo_are_set(self) -> None:
        backend = self._engine("ghp_x").build_backend()
        self.assertEqual(type(backend).__name__, "ApiGitBackend")
        self.assertEqual(backend.owner, "owner")
        self.assertEqual(backend.branch, "main")

    def test_falls_back_to_git_without_token(self) -> None:
        backend = self._engine(None).build_backend()
        self.assertEqual(type(backend).__name__, "GitBackend")

    def test_falls_back_to_git_without_repository(self) -> None:
        # 仓库名留空不算「没配置」：会兜底成 school-files。
        self.config.repository.owner = ""
        self.assertEqual(
            type(self._engine("ghp_x").build_backend()).__name__, "GitBackend")

    def test_blank_repository_name_uses_default(self) -> None:
        self.config.repository.name = ""
        backend = self._engine("ghp_x").build_backend()
        self.assertEqual(type(backend).__name__, "ApiGitBackend")
        self.assertEqual(backend.name, "school-files")

    def test_force_git_overrides_api(self) -> None:
        engine = self._engine("ghp_x")
        self.assertEqual(
            type(engine.build_backend(force_git=True)).__name__, "GitBackend")

    def test_api_backend_accepts_tls_repair_tuple(self) -> None:
        """TLS 自愈只跟 git 后端有关，在线接口不该被它弄坏。"""
        engine = self._engine("ghp_x")
        engine._tls_repair = ("openssl", "C:/tmp/ca.pem")
        backend = engine.build_backend()
        self.assertEqual(type(backend).__name__, "ApiGitBackend")


if __name__ == "__main__":
    unittest.main(verbosity=2)
