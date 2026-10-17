"""ArchiveMate 自测：核心逻辑单元测试 + 端到端（本地 bare 仓库当远端）。

运行：python -m unittest discover -s tests -v
也可以用 python tests/test_archivemate.py 直接跑。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivemate import app_paths, classify  # noqa: E402
from archivemate.config import Config, normalize_extension  # noqa: E402
from archivemate.errors import ConfigError, FileTooLarge, GitError  # noqa: E402
from archivemate.fsutil import (  # noqa: E402
    BLOCK_BYTES, WARN_BYTES, check_size, describe_size_limit, human_size,
    unique_path,
)
from archivemate.git_backend import GitBackend, find_git  # noqa: E402
from archivemate.sync import (  # noqa: E402
    build_commit_message, chunk_by_size, unique_dest_rel,
)
from archivemate.tracker import (  # noqa: E402
    STATUS_BLOCKED, STATUS_PENDING, PendingItem, Tracker,
)

SANDBOX = ROOT / ".testrun"
SANDBOX.mkdir(parents=True, exist_ok=True)
GIT = find_git("")


class RecordingPopen:
    """假的子进程：只把命令行和 env 记下来，不真的执行。

    run_command 现在走 Popen（为了能可靠地设超时），所以捕获类测试
    要拦 Popen，而不是已不再使用的 subprocess.run。
    """

    records: list[dict] = []
    next_returncode = 0
    next_stdout = b""
    next_stderr = b""

    def __init__(self, command, **kwargs):
        self.command = list(command)
        self.pid = 0
        self.returncode = RecordingPopen.next_returncode
        RecordingPopen.records.append({
            "argv": list(command),
            "env": dict(kwargs.get("env") or {}),
            "kwargs": dict(kwargs),
        })
        # run_command 会把真实临时文件当 stdout/stderr 传进来，写点内容进去，
        # 这样读回来就跟真进程一样。
        for key, payload in (("stdout", RecordingPopen.next_stdout),
                             ("stderr", RecordingPopen.next_stderr)):
            stream = kwargs.get(key)
            if stream is not None and payload:
                try:
                    stream.write(payload)
                    stream.flush()
                except Exception:  # noqa: BLE001
                    pass

    def wait(self, timeout=None):  # noqa: ARG002
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass

    @classmethod
    def reset(cls):
        cls.records = []
        cls.next_returncode = 0
        cls.next_stdout = b""
        cls.next_stderr = b""


def _make_item(name: str, size: int, mtime: float, dest: str) -> PendingItem:
    return PendingItem(
        item_id=name, source=str(Path("x") / name), size=size, mtime=mtime,
        dest_rel=dest, added_at=time.time())


def _mkdtemp(prefix: str = "t-") -> Path:
    """在沙箱目录下建一个临时目录。

    刻意不用 tempfile.mkdtemp：它按 0o700 建目录，在 Windows 上会写出一份
    收紧的 DACL，导致后续在这个目录里再建子目录被拒绝（沙箱环境下尤甚）。
    """
    global _COUNTER
    _COUNTER += 1
    target = SANDBOX / f"{prefix}{os.getpid()}-{_COUNTER}"
    target.mkdir(parents=True, exist_ok=True)
    return target


_COUNTER = 0


def _touch(path: Path, payload: bytes, when: str) -> Path:
    """写文件并把 mtime 固定到指定时刻，让分类结果可预测。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M"))
    os.utime(path, (stamp, stamp))
    return path


# ======================================================================
# 分类
# ======================================================================


class TestClassify(unittest.TestCase):
    def test_type_dirs(self):
        cases = {
            "a.pdf": "pdf", "a.PPTX": "ppt", "a.doc": "doc", "a.docx": "doc",
            "a.xlsx": "excel", "a.zip": "archive", "a.png": "image",
            "a.mp4": "video", "a.mp3": "audio", "a.md": "txt",
            "a.zzz": "zzz", "noext": "other",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify.type_dir_for(name), expected)

    def test_build_dest_rel_uses_mtime(self):
        tmp = _mkdtemp("cls-")
        self.addCleanup(shutil.rmtree, tmp, True)
        source = _touch(tmp / "数学课件.pptx", b"x", "2026-10-11 09:30")
        dest = classify.build_dest_rel(source)
        self.assertEqual(dest, "2026/10/ppt/数学课件.pptx")

    def test_build_dest_rel_now_override(self):
        when = datetime(2026, 1, 2, 9, 30)
        dest = classify.build_dest_rel(Path("c.pdf"), date_source="now", now=when)
        self.assertEqual(dest, "2026/01/pdf/c.pdf")

    def test_sanitize_windows_reserved(self):
        cleaned = classify.sanitize_filename('CON<>:"/\\|?*.pdf')
        self.assertNotIn("<", cleaned)
        self.assertNotIn("/", cleaned)
        self.assertTrue(cleaned.endswith(".pdf"))
        self.assertEqual(classify.sanitize_filename("CON.pdf"), "_CON.pdf")
        self.assertEqual(classify.sanitize_filename(""), "未命名文件")

    def test_human_category(self):
        self.assertIn("/", classify.human_category("2026/10/ppt/a.pptx"))


# ======================================================================
# 体积限制
# ======================================================================


class TestSizeLimits(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1536), "1.5 KB")

    def test_thresholds(self):
        self.assertEqual(describe_size_limit(WARN_BYTES - 1), "")
        self.assertTrue(describe_size_limit(WARN_BYTES))
        self.assertIn("已阻止", describe_size_limit(BLOCK_BYTES))

    def test_check_size_raises(self):
        check_size(WARN_BYTES)          # 只是警告，不抛
        with self.assertRaises(FileTooLarge):
            check_size(BLOCK_BYTES, hard=True)


# ======================================================================
# 配置
# ======================================================================


class TestConfig(unittest.TestCase):
    def test_normalize_extension(self):
        for raw in ("PDF", "*.pdf", " .Pdf "):
            self.assertEqual(normalize_extension(raw), ".pdf")
        self.assertEqual(normalize_extension("tar.gz"), ".gz")
        self.assertEqual(normalize_extension("   "), "")

    def test_roundtrip(self):
        cfg = Config.default()
        cfg.file_types = ["pdf", "PDF", ".docx"]
        cfg.watch_folders = ["a", "a", "b"]
        cfg.normalize()
        self.assertEqual(cfg.file_types, [".pdf", ".docx"])
        self.assertEqual(len(cfg.watch_folders), 2)

        again = Config.from_dict(cfg.to_dict())
        self.assertEqual(again.file_types, cfg.file_types)
        self.assertEqual(again.repository.branch, cfg.repository.branch)

    def test_bad_interval_falls_back(self):
        cfg = Config.from_dict({"sync_interval_minutes": 7})
        self.assertEqual(cfg.sync_interval_minutes, 0)

    def test_empty_types_get_defaults(self):
        cfg = Config.from_dict({"file_types": []})
        self.assertIn(".pdf", cfg.file_types)

    def test_repository_url(self):
        cfg = Config()
        self.assertFalse(cfg.repository.is_configured())
        cfg.repository.owner, cfg.repository.name = "me", "school"
        self.assertEqual(cfg.repository.slug, "me/school")
        self.assertEqual(cfg.repository.resolved_remote_url(),
                         "https://github.com/me/school.git")
        cfg.repository.remote_url = "D:/tmp/x.git"
        self.assertEqual(cfg.repository.resolved_remote_url(), "D:/tmp/x.git")

    def test_matches_type(self):
        cfg = Config()
        self.assertTrue(cfg.matches_type("A.PDF"))
        self.assertFalse(cfg.matches_type("A.txt"))
        self.assertFalse(cfg.matches_type("noextension"))


# ======================================================================
# 队列状态
# ======================================================================


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = _mkdtemp("tracker-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = self.tmp / "downloads" / "a.pdf"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"hello world")

    def test_is_known_requires_matching_signature(self):
        tracker = Tracker(self.tmp / "state.json")
        self.assertFalse(tracker.is_known(self.target))
        tracker.remember(self.target)
        self.assertTrue(tracker.is_known(self.target))

        # 同名文件被重新下载（大小/时间变了）应被当成新文件
        time.sleep(0.01)
        self.target.write_bytes(b"hello world, but longer")
        self.assertFalse(tracker.is_known(self.target),
                         "增大的同名文件必须被重新识别")

    def test_is_known_false_when_missing(self):
        tracker = Tracker(self.tmp / "state.json")
        tracker.remember(self.target)
        self.target.unlink()
        self.assertFalse(tracker.is_known(self.target))

    def test_persistence_roundtrip(self):
        path = self.tmp / "state.json"
        tracker = Tracker(path)
        item = tracker.add(self.target, dest_rel="2026/10/pdf/a.pdf")
        tracker.record_success(item, str(self.tmp / "archive"))
        tracker.remove(item.item_id)
        tracker.save()

        again = Tracker(path)
        self.assertEqual(len(again.items), 0)
        self.assertEqual(len(again.history), 1)
        self.assertEqual(again.history[0].dest_rel, "2026/10/pdf/a.pdf")
        self.assertEqual(again.stats["pushed_files"], 1)

    def test_status_filters(self):
        tracker = Tracker(self.tmp / "state.json")
        tracker.add(self.target, dest_rel="a.pdf")
        b = self.tmp / "downloads" / "b.pdf"
        b.write_bytes(b"x")
        tracker.add(b, dest_rel="b.pdf", status=STATUS_BLOCKED)
        self.assertEqual(len(tracker.pending_items()), 1)
        self.assertEqual(len(tracker.retryable_items()), 1)

    def test_prune_seen(self):
        tracker = Tracker(self.tmp / "state.json")
        tracker.remember(self.target)
        self.target.unlink()
        self.assertEqual(tracker.prune_seen(), 1)
        self.assertEqual(tracker.seen, {})


# ======================================================================
# 同步辅助
# ======================================================================


class TestSyncHelpers(unittest.TestCase):
    def test_unique_dest_rel(self):
        self.assertEqual(unique_dest_rel("a/b.pdf", set()), "a/b.pdf")
        self.assertEqual(unique_dest_rel("a/b.pdf", {"a/b.pdf"}), "a/b (2).pdf")
        self.assertEqual(unique_dest_rel("b.pdf", {"b.pdf"}), "b (2).pdf")

    def test_commit_message_single(self):
        when = time.mktime(time.strptime("2026-10-11 09:30", "%Y-%m-%d %H:%M"))
        item = _make_item("x", 10, when, "2026/10/ppt/数学课件.pptx")
        self.assertEqual(build_commit_message([item]),
                         "add: 2026-10-11 数学课件.pptx")

    def test_commit_message_many(self):
        when = time.time()
        items = [_make_item(f"f{i}", 10, when, f"2026/10/pdf/f{i}.pdf")
                 for i in range(3)]
        text = build_commit_message(items)
        self.assertIn("3 个文件", text)
        self.assertIn("2026/10/pdf/f0.pdf", text)

    def test_chunk_by_size(self):
        items = [_make_item(f"f{i}", 60, 0.0, f"f{i}") for i in range(5)]
        chunks = chunk_by_size(items, 200)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([len(c) for c in chunks], [3, 2])
        self.assertEqual(len(chunk_by_size(items, 10_000)), 1)


# ======================================================================
# 端到端：本地 bare 仓库当远端
# ======================================================================


class LocalOnlyBackend(GitBackend):
    """把「远端」换成同一个本地 bare 仓库的测试后端。

    本沙箱禁止创建命名管道，Git for Windows 的本地/file 传输需要 spawn
    ``sh.exe``（报 ``couldn't create signal pipe, Win32 error 5``），所以
    任何指向本地路径的 push/fetch/ls-remote 都会失败。这里让 repo_dir 直接
    就是那个 bare 仓库，只用本地 ref 查询代替传输——blob/树/提交信息/排重/
    归档/队列这些真正的逻辑仍然被真实地跑过一遍。
    """

    def remote_branch_exists(self) -> bool:
        result = self._run(["rev-parse", "--verify", "--quiet",
                            f"refs/heads/{self.branch}"], check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    def fetch(self) -> None:
        self.ensure_repo()
        result = self._run(["rev-parse", "--verify", "--quiet",
                            f"refs/heads/{self.branch}"], check=False)
        self._base_commit = result.stdout.strip() or None

    def push(self) -> None:
        # 远端就是这个仓库本身，对象与 ref 已经写好了
        return None


@unittest.skipIf(not GIT, "需要安装 git 才能跑端到端测试")
class TestEndToEnd(unittest.TestCase):
    """分类 → 提交 → 推送 → 归档 的完整链路，用本地 bare 仓库替代 GitHub。"""

    def setUp(self):
        self.base = _mkdtemp("e2e-")
        self.addCleanup(shutil.rmtree, self.base, True)

        self.home = self.base / "home"
        self.downloads = self.base / "downloads"
        self.archive = self.base / "archive"
        self.remote = self.base / "remote.git"
        for directory in (self.home, self.downloads):
            directory.mkdir(parents=True, exist_ok=True)

        self._old_home = os.environ.get("ARCHIVEMATE_HOME")
        os.environ["ARCHIVEMATE_HOME"] = str(self.home)
        app_paths.reset_cache()

        subprocess.run([GIT, "init", "--bare", "--initial-branch", "main",
                        str(self.remote)], check=True, capture_output=True)

        from archivemate.service import ArchiveMateService

        cfg = Config.default()
        cfg.watch_folders = [str(self.downloads)]
        cfg.archive_folder = str(self.archive)
        cfg.repository.remote_url = str(self.remote)
        cfg.repository.branch = "main"
        cfg.git.author_name = "Test Runner"
        cfg.git.author_email = "test@localhost"
        cfg.limits.warn_bytes = 500
        cfg.limits.block_bytes = 1000
        cfg.limits.batch_bytes = 4000
        cfg.normalize()
        self.cfg = cfg
        self.service = ArchiveMateService(
            cfg, auto_start_watcher=False,
            backend_factory=self._make_backend,
        )

    def _make_backend(self) -> LocalOnlyBackend:
        return LocalOnlyBackend(
            self.remote,
            self.cfg.repository.resolved_remote_url(),
            self.cfg.repository.branch,
            git_exe=GIT,
            author_name=self.cfg.git.author_name,
            author_email=self.cfg.git.author_email,
        )

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ARCHIVEMATE_HOME", None)
        else:
            os.environ["ARCHIVEMATE_HOME"] = self._old_home
        app_paths.reset_cache()

    # ------------------------------------------------------------------

    def _write(self, name: str, payload: bytes, when: str = "2026-10-11 09:30"):
        path = self.downloads / name
        path.write_bytes(payload)
        stamp = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M"))
        os.utime(path, (stamp, stamp))
        return path

    def _git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run([GIT, f"--git-dir={self.remote}", *args],
                                check=check, capture_output=True, text=True,
                                encoding="utf-8")
        return result.stdout

    def _remote_paths(self) -> list[str]:
        """远端 main 分支里的文件路径；分支还不存在时返回空列表。"""
        exists = self._git("rev-parse", "--verify", "--quiet", "refs/heads/main",
                           check=False)
        if not exists.strip():
            return []
        # -z：路径不做转义，中文文件名才是原样
        raw = self._git("ls-tree", "-r", "-z", "--name-only", "main")
        return [entry for entry in raw.split("\0") if entry]

    def test_full_pipeline(self):
        self._write("数学课件.pptx", b"powerpoint bytes")
        self._write("作文模板.docx", b"word bytes")
        ignored = self._write("笔记.txt", b"should stay")
        huge = self._write("大文件.pdf", b"x" * 1500)

        # --- 入队 ---
        ok, reason = self.service.ingest(self.downloads / "数学课件.pptx")
        self.assertTrue(ok, reason)
        ok, reason = self.service.ingest(self.downloads / "作文模板.docx")
        self.assertTrue(ok, reason)

        rejected, why = self.service.ingest(ignored)
        self.assertFalse(rejected)
        self.assertIn("不在目标文件类型", why)

        ok, why = self.service.ingest(huge)
        self.assertTrue(ok, why)
        blocked = self.service.tracker.find_by_source(huge)
        self.assertEqual(blocked.status, STATUS_BLOCKED)

        self.assertEqual(len(self.service.tracker.items), 3)

        # --- 同步 ---
        outcome = self.service.sync_blocking()
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(outcome.pushed_count, 2, outcome.message)
        self.assertEqual(len(outcome.archived), 2)
        self.assertEqual(len(outcome.blocked), 1)

        # --- 远端仓库内容 ---
        tree = self._remote_paths()
        self.assertIn("2026/10/ppt/数学课件.pptx", tree)
        self.assertIn("2026/10/doc/作文模板.docx", tree)
        self.assertNotIn("笔记.txt", "".join(tree))

        log = self._git("log", "--format=%s", "main").strip()
        self.assertTrue(log.startswith("add: 2026-10-11"), log)

        # --- 归档 ---
        self.assertTrue((self.archive / "2026/10/ppt/数学课件.pptx").is_file())
        self.assertTrue((self.archive / "2026/10/doc/作文模板.docx").is_file())
        self.assertFalse((self.downloads / "数学课件.pptx").exists(),
                         "推送成功后源文件应已移走")
        self.assertTrue(ignored.is_file(), "非目标类型文件不能被动")
        self.assertTrue(huge.is_file(), "被阻止的文件不能被动")

        # --- 队列与历史 ---
        remaining = {item.dest_rel for item in self.service.tracker.items}
        self.assertEqual(remaining, {"2026/10/pdf/大文件.pdf"})
        self.assertEqual(len(self.service.tracker.history), 2)
        self.assertEqual(self.service.tracker.stats["pushed_files"], 2)

        # --- 状态落盘 ---
        state = json.loads((self.home / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["history"]), 2)
        self.assertNotIn("token", json.dumps(state).lower())

    def test_no_token_needed_for_local_remote(self):
        self.assertEqual(self.service.engine.validate(), [])

    def test_blocked_file_is_not_pushed(self):
        self._write("大文件.pdf", b"x" * 1500)
        self.service.ingest(self.downloads / "大文件.pdf")
        outcome = self.service.sync_blocking()
        self.assertEqual(outcome.pushed_count, 0)
        self.assertEqual(len(outcome.blocked), 1)
        self.assertFalse(outcome.ok)
        self.assertEqual(self._remote_paths(), [])

    def test_duplicate_gets_renamed_not_overwritten(self):
        first = self._write("同名.pdf", b"first version")
        self.service.ingest(first)
        self.service.sync_blocking()
        self.assertIn("2026/10/pdf/同名.pdf", self._remote_paths())

        # 再放一个同名但内容不同的文件（模拟重新下载）
        self._write("同名.pdf", b"second version, different")
        self.service.ingest(self.downloads / "同名.pdf")
        outcome = self.service.sync_blocking()
        self.assertTrue(outcome.ok, outcome.message)

        tree = self._remote_paths()
        self.assertIn("2026/10/pdf/同名.pdf", tree)
        self.assertIn("2026/10/pdf/同名 (2).pdf", tree)

    def test_retry_after_source_deleted(self):
        path = self._write("会消失.pdf", b"bye")
        self.service.ingest(path)
        path.unlink()
        outcome = self.service.sync_blocking()
        self.assertEqual(outcome.pushed_count, 0)
        item = self.service.tracker.items[0]
        self.assertEqual(item.status, "missing")

    def test_clear_archive_guard(self):
        self.assertTrue(self.service._archive_clear_guard(Path("C:\\")))
        self.assertEqual(self.service._archive_clear_guard(self.archive), "")

    def test_archive_clear_removes_content(self):
        (self.archive / "2026/10/pdf").mkdir(parents=True)
        (self.archive / "2026/10/pdf/a.pdf").write_bytes(b"12345")
        removed, freed = self.service.clear_archive()
        self.assertEqual(removed, 1)
        self.assertEqual(freed, 5)
        self.assertTrue(self.archive.is_dir(), "归档目录本身要保留")
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_second_sync_is_noop(self):
        self._write("一次.pdf", b"only once")
        self.service.ingest(self.downloads / "一次.pdf")
        self.service.sync_blocking()
        outcome = self.service.sync_blocking()
        self.assertEqual(outcome.pushed_count, 0)
        self.assertIn("没有待推送", outcome.message)


class TestCredentialHygiene(unittest.TestCase):
    """需求第 8 节与「隐私安全」：凭据不得硬编码、不得明文落盘、不得进命令行。

    这几条是硬约束，出问题不会有报错、只会静悄悄泄露，所以单独用测试钉住。
    """

    SECRET = "ghp_ThisIsAFakeTokenForTesting0123456789"

    def setUp(self):
        self.home = _mkdtemp("cred-")
        self.backend = GitBackend(
            self.home / "repo.git",
            "https://github.com/alice/homework.git",
            "main",
            git_exe="git",
            author_name="ArchiveMate",
            author_email="archivemate@localhost",
            token=self.SECRET,
            proxy="http://127.0.0.1:7890",
        )
        self.calls: list[dict] = []

    def _fake_run(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        return subprocess.CompletedProcess(list(argv), 0, b"", b"")

    def _run_with_capture(self, action) -> dict:
        RecordingPopen.reset()
        with mock.patch("archivemate.git_backend.subprocess.Popen",
                        RecordingPopen):
            action()
        self.assertTrue(RecordingPopen.records, "没有捕获到任何 git 调用")
        self.calls = RecordingPopen.records
        return RecordingPopen.records[-1]

    # -- Token 不能出现在命令行 -------------------------------------------

    def test_token_never_appears_in_argv(self):
        call = self._run_with_capture(self.backend.push)
        self.assertNotIn(self.SECRET, " ".join(call["argv"]),
                         "Token 出现在了 git 命令行里，ps/tasklist 能看到")

    def test_token_never_appears_in_env_dump_except_extraheader(self):
        """Token 只允许以 base64 形式出现在 http.extraheader 这一个键里。"""
        call = self._run_with_capture(self.backend.push)
        for key, value in call["env"].items():
            if key == "GIT_CONFIG_VALUE_0":
                continue
            self.assertNotIn(self.SECRET, value, f"{key} 里出现了明文 Token")
            self.assertNotIn(
                base64.b64encode(self.SECRET.encode()).decode(), value,
                f"{key} 里出现了 Token 的 base64")

    def test_extraheader_is_well_formed_basic_auth(self):
        call = self._run_with_capture(self.backend.push)
        env = call["env"]
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraheader")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        header = env["GIT_CONFIG_VALUE_0"]
        self.assertTrue(header.startswith("Authorization: Basic "))
        decoded = base64.b64decode(header.split(" ", 2)[2]).decode("utf-8")
        self.assertEqual(decoded, f"x-access-token:{self.SECRET}")

    def test_interactive_prompts_are_disabled(self):
        """否则卡在密码提示上，或者把 Token 交给 git 的凭据助手存下来。"""
        call = self._run_with_capture(self.backend.push)
        env = call["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "Never")
        self.assertIn("-c", call["argv"])
        self.assertIn("credential.helper=", call["argv"])

    # -- 代理 --------------------------------------------------------------

    def test_proxy_reaches_git_two_ways(self):
        call = self._run_with_capture(self.backend.push)
        self.assertIn("http.proxy=http://127.0.0.1:7890", call["argv"])
        self.assertEqual(call["env"]["HTTPS_PROXY"], "http://127.0.0.1:7890")

    def test_no_token_means_no_extraheader_env(self):
        backend = GitBackend(self.home / "repo2.git",
                             "https://github.com/alice/homework.git", "main",
                             git_exe="git", token="")
        env = backend._base_env()
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertNotIn("GIT_CONFIG_VALUE_0", env)

    # -- 不落盘 ------------------------------------------------------------

    def test_config_file_never_contains_the_token(self):
        from archivemate.config import save

        config = Config.default()
        config.repository.owner = "alice"
        config.repository.name = "homework"
        config.archive_folder = str(self.home / "archive")
        path = self.home / "config.json"
        save(config, path)

        text = path.read_text(encoding="utf-8")
        self.assertNotIn(self.SECRET, text)

        def keys_of(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from keys_of(value)
            elif isinstance(node, list):
                for value in node:
                    yield from keys_of(value)

        # 允许文档注释里出现「Token」这个词，但绝不允许真的有一个存 token 的字段
        self.assertNotIn("token", {key.lower() for key in keys_of(json.loads(text))},
                         "配置文件里不该有任何存 token 的字段")
        self.assertIn("repository", json.loads(text))

    def test_state_file_never_contains_the_token(self):
        tracker = Tracker(self.home / "state.json")
        source = self.home / "a.pdf"
        source.write_bytes(b"x")
        tracker.add(source, dest_rel="2026/10/pdf/a.pdf")
        tracker.save()

        text = (self.home / "state.json").read_text(encoding="utf-8")
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn("ghp_", text)

    def test_source_tree_has_no_hardcoded_token(self):
        pattern = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "archivemate").rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], f"这些文件里疑似硬编码了 Token：{offenders}")

    def test_git_error_message_never_echoes_the_token(self):
        """报错信息最后会进日志文件，不能带 Token。"""
        backend = GitBackend(self.home / "repo3.git",
                             "https://github.com/alice/homework.git", "main",
                             git_exe="git", token=self.SECRET)

        RecordingPopen.reset()
        RecordingPopen.next_returncode = 128
        RecordingPopen.next_stderr = (
            f"fatal: Authentication failed for https://alice:{self.SECRET}@github.com/x"
        ).encode()

        with mock.patch("archivemate.git_backend.subprocess.Popen", RecordingPopen):
            with self.assertRaises(GitError) as ctx:
                backend.push()
        self.assertNotIn(self.SECRET, str(ctx.exception))


class TestTransportDiagnostics(unittest.TestCase):
    """HTTPS 传输层的报错要说人话。

    起因：探测真实 GitHub 连接时发现 `_friendly_error` 把「证书校验失败」
    误报成「检查网络/代理」——因为 git 给所有传输错误都套了 "unable to access"。
    用户照着改代理只会白折腾。
    """

    def setUp(self):
        self.home = _mkdtemp("tls-")

    def _backend(self, **kwargs) -> GitBackend:
        return GitBackend(self.home / "r.git", "https://github.com/a/b.git", "main",
                          git_exe="git", **kwargs)

    def _error_for(self, stderr: str) -> str:
        result = subprocess.CompletedProcess(["git"], 128, b"", stderr.encode("utf-8"))
        return self._backend()._friendly_error(result)

    # -- 证书 / TLS --------------------------------------------------------

    def test_certificate_failure_is_not_blamed_on_the_network(self):
        message = self._error_for(
            "fatal: unable to access 'https://github.com/a/b.git/': "
            "SSL certificate problem: unable to get local issuer certificate")
        self.assertIn("证书", message)
        self.assertIn("OpenSSL", message)
        self.assertNotIn("检查网络", message,
                         "证书问题不该让用户去查网络")

    def test_schannel_credential_failure_gets_the_same_way_out(self):
        message = self._error_for(
            "fatal: unable to access 'https://github.com/a/b.git/': "
            "schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS")
        self.assertIn("schannel", message)
        self.assertIn("OpenSSL", message)

    def test_dns_failure_still_points_at_the_network(self):
        message = self._error_for(
            "fatal: unable to access 'https://github.com/a/b.git/': "
            "Could not resolve host: github.com")
        self.assertIn("github.com", message)
        self.assertNotIn("证书", message)

    def test_auth_and_not_found_keep_their_own_messages(self):
        self.assertIn("Token", self._error_for(
            "remote: Invalid username or password.\n"
            "fatal: Authentication failed for 'https://github.com/a/b.git/'"))
        self.assertIn("仓库", self._error_for(
            "remote: Repository not found.\n"
            "fatal: repository 'https://github.com/a/b.git/' not found"))

    def test_generic_transport_failure_mentions_the_proxy_setting(self):
        message = self._error_for(
            "fatal: unable to access 'https://github.com/a/b.git/': "
            "Failed to connect to github.com port 443: Timed out")
        self.assertIn("代理", message)

    # -- ssl_backend 传递 ---------------------------------------------------

    def test_ssl_backend_reaches_the_git_command_line(self):
        backend = self._backend(ssl_backend="openssl")
        RecordingPopen.reset()
        with mock.patch("archivemate.git_backend.subprocess.Popen", RecordingPopen):
            backend.push()
        self.assertIn("http.sslBackend=openssl", RecordingPopen.records[-1]["argv"])

    def test_ssl_backend_left_empty_by_default(self):
        backend = self._backend()
        RecordingPopen.reset()
        with mock.patch("archivemate.git_backend.subprocess.Popen", RecordingPopen):
            backend.push()
        self.assertNotIn("http.sslBackend=", " ".join(RecordingPopen.records[-1]["argv"]))

    def test_only_known_ssl_backends_are_accepted(self):
        self.assertEqual(self._backend(ssl_backend="openssl").ssl_backend, "openssl")
        self.assertEqual(self._backend(ssl_backend="OpenSSL").ssl_backend, "openssl")
        self.assertEqual(self._backend(ssl_backend="junk").ssl_backend, "")
        self.assertEqual(self._backend(ssl_backend="").ssl_backend, "")

    def test_config_roundtrip_preserves_ssl_backend(self):
        config = Config.default()
        config.git.ssl_backend = "openssl"
        restored = Config.from_dict(json.loads(json.dumps(config.to_dict())))
        self.assertEqual(restored.git.ssl_backend, "openssl")
        self.assertEqual(restored.git.resolved_ssl_backend(), "openssl")

    def test_unknown_ssl_backend_in_config_is_ignored(self):
        config = Config.default()
        config.git.ssl_backend = "../evil"
        self.assertEqual(config.git.resolved_ssl_backend(), "")


class _FakeResponse:
    """假装成 urllib 的响应对象。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class TestGitHubClient(unittest.TestCase):
    """GitHub REST 客户端。

    回归来源：用户在首次引导里点「创建私有仓库」，弹出
    「GitHubClient.__init__() takes 2 positional arguments but 3 were given」。
    这条路径此前完全没有测试覆盖——proxy 是 keyword-only，而调用方按位置传。
    """

    def setUp(self):
        from archivemate.github_api import GitHubClient

        self.GitHubClient = GitHubClient
        self.client = GitHubClient("ghp_fake", proxy="http://127.0.0.1:7890")
        self.requests: list = []
        self.response = _FakeResponse(b"{}")
        self._open_errors: list = []
        self.addCleanup(self._close_errors)

    def _close_errors(self):
        for error in self._open_errors:
            try:
                error.close()
            except Exception:
                pass
        self._open_errors.clear()

    def _patch_opener(self):
        def fake_open(request, timeout=None):
            self.requests.append(request)
            return self.response

        return mock.patch.object(self.client._opener, "open", side_effect=fake_open)

    def _call(self, fn):
        with self._patch_opener():
            return fn()

    # -- 请求构造 ---------------------------------------------------------

    def test_authorization_and_api_headers(self):
        self.response = _FakeResponse(b'{"login": "alice", "name": "Alice"}')
        user = self._call(self.client.get_user)
        self.assertEqual(user.login, "alice")
        self.assertEqual(user.display, "Alice")

        request = self.requests[-1]
        self.assertEqual(request.get_header("Authorization"), "Bearer ghp_fake")
        self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")
        self.assertTrue(request.get_header("User-agent").startswith("ArchiveMate/"))

    def test_create_repository_is_private_and_auto_initialised(self):
        self.response = _FakeResponse(json.dumps({
            "owner": {"login": "alice"}, "name": "school-files",
            "private": True, "default_branch": "main",
            "clone_url": "https://github.com/alice/school-files.git",
        }).encode())
        info = self._call(lambda: self.client.create_repository(
            "school-files", description="归档"))

        request = self.requests[-1]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/user/repos"))
        payload = json.loads(request.data.decode("utf-8"))
        # auto_init 必须有：否则新仓库没有任何分支，后续 fetch 会失败
        self.assertTrue(payload["auto_init"])
        self.assertTrue(payload["private"])
        self.assertEqual(payload["name"], "school-files")

        self.assertEqual(info.slug, "alice/school-files")
        self.assertEqual(info.clone_url, "https://github.com/alice/school-files.git")

    def test_list_repositories_reads_owner_object(self):
        self.response = _FakeResponse(json.dumps([{
            "owner": {"login": "alice"}, "name": "a", "private": True,
            "default_branch": "trunk", "clone_url": "u",
        }]).encode())
        repos = self._call(lambda: self.client.list_repositories())
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0].owner, "alice")
        self.assertEqual(repos[0].default_branch, "trunk")

    def test_empty_body_returns_none(self):
        self.response = _FakeResponse(b"")
        self.assertIsNone(self._call(lambda: self.client._request("GET", "/x")))

    def test_proxy_is_trimmed(self):
        plain = self.GitHubClient("t", proxy="  http://127.0.0.1:7890  ")
        self.assertEqual(plain.proxy, "http://127.0.0.1:7890")

    # -- 错误翻译 ---------------------------------------------------------

    def _http_error(self, code: int, message: str):
        import io
        import urllib.error

        body = io.BytesIO(json.dumps({"message": message}).encode("utf-8"))
        error = urllib.error.HTTPError("https://api.github.com/x", code, "err",
                                       {}, body)
        self._open_errors.append(error)
        return error

    def test_status_codes_are_explained_in_chinese(self):
        cases = {401: "Token", 403: "repo", 404: "找不到", 422: "已存在"}
        for code, needle in cases.items():
            with self.subTest(code=code):
                exc = self._http_error(code, "school-files 已存在")
                self.assertIn(needle, self.GitHubClient._explain(exc))

    def test_http_error_becomes_github_error_with_status(self):
        from archivemate.errors import GitHubError

        def raise_401(request, timeout=None):
            raise self._http_error(401, "Bad credentials")

        with mock.patch.object(self.client._opener, "open", side_effect=raise_401):
            with self.assertRaises(GitHubError) as ctx:
                self.client.get_user()
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("Token", str(ctx.exception))

    def test_network_failure_mentions_proxy(self):
        import urllib.error

        from archivemate.errors import GitHubError

        def raise_urlerror(request, timeout=None):
            raise urllib.error.URLError("timed out")

        with mock.patch.object(self.client._opener, "open", side_effect=raise_urlerror):
            with self.assertRaises(GitHubError) as ctx:
                self.client.get_user()
        self.assertIn("代理", str(ctx.exception))

    def test_garbage_body_is_reported(self):
        from archivemate.errors import GitHubError

        self.response = _FakeResponse(b"<html>not json</html>")
        with self.assertRaises(GitHubError):
            self._call(lambda: self.client._request("GET", "/x"))


class TestKeychainFallback(unittest.TestCase):
    """密钥链不可用时只能降级到内存，绝不允许退化成明文落盘。"""

    def setUp(self):
        from archivemate import credentials

        self.credentials = credentials
        self.target = f"ArchiveMate/test-{os.getpid()}-{id(self)}"
        self.addCleanup(self.credentials.delete_token, self.target)
        self.credentials.delete_token(self.target)

    def test_unavailable_backend_raises_instead_of_writing_plaintext(self):
        with mock.patch.object(self.credentials, "_backend", return_value=None):
            with self.assertRaises(self.credentials.CredentialError):
                self.credentials.set_token("ghp_fake", self.target)

    def test_session_fallback_keeps_token_in_memory_only(self):
        with mock.patch.object(self.credentials, "_backend", return_value=None):
            self.credentials.remember_for_session("ghp_mem", self.target)
            self.assertTrue(self.credentials.has_session_token(self.target))
            self.assertEqual(self.credentials.get_token(self.target), "ghp_mem")

    def test_failed_write_does_not_fall_back_to_disk(self):
        def boom(key, token, account):
            raise self.credentials.CredentialError("模拟密钥链写入失败")

        with mock.patch.object(self.credentials, "_backend",
                               return_value=(boom, lambda k: None, lambda k: None)):
            with self.assertRaises(self.credentials.CredentialError):
                self.credentials.set_token("ghp_fake", self.target)
            # 写入失败后不应留下任何痕迹
            self.assertFalse(self.credentials.has_session_token(self.target))

    def test_session_only_token_produces_a_warning_hint(self):
        with mock.patch.object(self.credentials, "is_available", return_value=False):
            hint = self.credentials.storage_hint()
            self.assertIn("内存", hint)
            self.assertNotIn("凭据管理器", hint)


class _BlackHoleServer:
    """一个只 accept、永不回数据的本地服务器，用来模拟「连接被黑洞掉」。

    这是「点同步就永久卡死」的最小复现装置：TCP 连得上，但握手数据永远不来。
    """

    def __init__(self) -> None:
        import socket
        import threading
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.port = self._socket.getsockname()[1]
        self._accepted: list = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._socket.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except OSError:
                continue
            # 故意什么都不发，也不关掉连接
            self._accepted.append(conn)

    def url(self, name: str = "x.git") -> str:
        # 故意用 http 而不是 https：https 在本机可能因为证书后端问题秒失败，
        # 那样就绕过了「连上但数据不来」这个真正要复现的场景。
        return f"http://127.0.0.1:{self.port}/{name}"

    def close(self) -> None:
        self._stop.set()
        for conn in self._accepted:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=3)


class TestRunCommandNeverHangs(unittest.TestCase):
    """回归：子进程派生孙进程、孙进程一直占着输出管道时也不能永久卡住。

    这正是 subprocess.run / communicate 的失效场景：超时只杀直接子进程，
    孙进程还握着管道写端，EOF 永远不来。旧代码会从「有超时」退化成「永久卡死」。
    """

    CHILD = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "time.sleep(600)\n"
    )

    def test_timeout_returns_promptly_despite_a_pipe_holding_grandchild(self):
        from archivemate.git_backend import run_command

        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_command([sys.executable, "-c", self.CHILD],
                        env=dict(os.environ), timeout=5)
        elapsed = time.monotonic() - started
        # 旧实现会一直等到孙进程自己睡醒（600s）
        self.assertLess(elapsed, 45, f"run_command 花了 {elapsed:.1f}s，没有按时放弃")

    def test_normal_command_captures_stdout_and_stderr(self):
        from archivemate.git_backend import run_command

        result = run_command(
            [sys.executable, "-c",
             "import sys; sys.stdout.write('out-好'); sys.stderr.write('err')"],
            env=dict(os.environ), timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode("utf-8"), "out-好")
        self.assertEqual(result.stderr.decode("utf-8"), "err")

    def test_stdin_payload_reaches_the_child(self):
        from archivemate.git_backend import run_command

        result = run_command(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            env=dict(os.environ), input_bytes="你好\n第二行\n".encode("utf-8"),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode("utf-8"), "你好\n第二行\n")

    def test_failing_command_reports_nonzero_without_raising(self):
        from archivemate.git_backend import run_command

        result = run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"],
            env=dict(os.environ), timeout=60,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr.decode("utf-8"), "bad")

    def test_kill_process_tree_tolerates_an_exited_process(self):
        from archivemate.git_backend import kill_process_tree

        process = subprocess.Popen([sys.executable, "-c", "pass"],
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        process.wait(timeout=30)
        kill_process_tree(process)  # 不应抛异常，也不应卡住


class TestStallProtection(unittest.TestCase):
    """回归：连接被黑洞掉时，git 必须自己放弃，而不是无限等待。"""

    def setUp(self):
        self.server = _BlackHoleServer()
        self.addCleanup(self.server.close)
        self.repo = _mkdtemp("stall-") / "repo.git"

    def _backend(self, **kwargs) -> GitBackend:
        defaults = dict(git_exe=GIT, network_timeout=20, logger=lambda _m: None)
        defaults.update(kwargs)
        backend = GitBackend(self.repo, self.server.url(), "main", **defaults)
        # 走真实入口：fetch() 会先 ensure_repo()，否则 git 会因为
        # origin 还没配置而秒失败，测试就绕过了要复现的停滞场景。
        backend.ensure_repo()
        return backend

    def test_git_gives_up_when_the_connection_stalls(self):
        backend = self._backend(stall_seconds=5)
        started = time.monotonic()
        with self.assertRaises(GitError) as caught:
            backend.fetch()
        elapsed = time.monotonic() - started
        # 必须真的等过了停滞窗口才放弃，而不是因为别的原因秒失败
        self.assertGreaterEqual(
            elapsed, 4.0,
            f"只用了 {elapsed:.1f}s，没有走到停滞保护那条路径")
        self.assertLess(elapsed, 90, f"黑洞连接下等了 {elapsed:.1f}s，没有放弃")
        # git 自己的说法：传输太慢
        self.assertIn("too slow", str(caught.exception).lower()
                      + str(getattr(caught.exception, "stderr", "")).lower())

    def test_hard_timeout_still_bounds_git_when_the_stall_guard_is_off(self):
        """两层保护：git 的 lowSpeed 保护关掉后，run_command 的硬超时仍然兜底。"""
        backend = self._backend(stall_seconds=0, network_timeout=6)
        started = time.monotonic()
        with self.assertRaises(GitError):
            backend.fetch()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 5.0, "硬超时没有生效")
        self.assertLess(elapsed, 60, f"硬超时没有兜住，等了 {elapsed:.1f}s")

    def test_stall_guard_is_passed_to_git(self):
        backend = self._backend(stall_seconds=7)
        captured: list[list[str]] = []
        real = subprocess.Popen

        def spy(command, **kwargs):
            captured.append(list(command))
            return real(command, **kwargs)

        with mock.patch("archivemate.git_backend.subprocess.Popen", side_effect=spy):
            with self.assertRaises(GitError):
                backend.fetch()

        flat = [" ".join(args) for args in captured]
        self.assertTrue(
            any("http.lowSpeedLimit=1000" in line for line in flat),
            f"没有把低速保护传给 git：{flat}",
        )
        self.assertTrue(
            any("http.lowSpeedTime=7" in line for line in flat),
            f"没有把停滞时限传给 git：{flat}",
        )

    def test_stall_guard_can_be_turned_off(self):
        backend = self._backend(stall_seconds=0, network_timeout=5)
        captured: list[list[str]] = []
        real = subprocess.Popen

        def spy(command, **kwargs):
            captured.append(list(command))
            return real(command, **kwargs)

        with mock.patch("archivemate.git_backend.subprocess.Popen", side_effect=spy):
            with self.assertRaises(GitError):
                backend.fetch()
        flat = [" ".join(args) for args in captured]
        self.assertFalse(any("lowSpeedTime" in line for line in flat))


class TestConfigTimeouts(unittest.TestCase):
    def test_defaults(self):
        from archivemate.config import GitConfig

        git = GitConfig()
        self.assertEqual(git.resolved_network_timeout(), 300)
        self.assertEqual(git.resolved_stall_timeout(), 60)

    def test_clamping(self):
        from archivemate.config import GitConfig

        git = GitConfig(network_timeout_seconds=5, stall_timeout_seconds=9999)
        self.assertEqual(git.resolved_network_timeout(), 30)
        self.assertEqual(git.resolved_stall_timeout(), 600)

    def test_zero_stall_means_off_and_garbage_falls_back(self):
        from archivemate.config import GitConfig

        self.assertEqual(GitConfig(stall_timeout_seconds=0).resolved_stall_timeout(), 0)
        self.assertEqual(
            GitConfig(network_timeout_seconds="x",
                      stall_timeout_seconds=None).resolved_network_timeout(), 300)
        self.assertEqual(
            GitConfig(network_timeout_seconds="x",
                      stall_timeout_seconds=None).resolved_stall_timeout(), 60)

    def test_roundtrip_through_json(self):
        from archivemate.config import GitConfig

        cfg = Config.default()
        cfg.git.network_timeout_seconds = 120
        cfg.git.stall_timeout_seconds = 45
        again = Config.from_dict(json.loads(json.dumps(cfg.to_dict())))
        self.assertEqual(again.git.resolved_network_timeout(), 120)
        self.assertEqual(again.git.resolved_stall_timeout(), 45)


class TestSyncCancellation(unittest.TestCase):
    """回归：同步卡在网络上时，用户能把它取消掉，而且能真的停下来。"""

    def setUp(self):
        self.base = _mkdtemp("cancel-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / "home"
        self.downloads = self.base / "downloads"
        self.home.mkdir(parents=True, exist_ok=True)
        self.downloads.mkdir(parents=True, exist_ok=True)

        self._old_home = os.environ.get("ARCHIVEMATE_HOME")
        os.environ["ARCHIVEMATE_HOME"] = str(self.home)
        app_paths.reset_cache()

        self.server = _BlackHoleServer()
        self.addCleanup(self.server.close)

        source = self.downloads / "讲义.pdf"
        source.write_bytes(b"%PDF-1.4 fake")
        stamp = datetime(2026, 10, 11, 9, 30).timestamp()
        os.utime(source, (stamp, stamp))

        cfg = Config.default()
        cfg.watch_folders = [str(self.downloads)]
        cfg.archive_folder = str(self.base / "archive")
        cfg.repository.remote_url = self.server.url()
        cfg.repository.branch = "main"
        # 让 git 自己不会提前放弃，从而真正考验「取消」这条路
        cfg.git.stall_timeout_seconds = 0
        cfg.git.network_timeout_seconds = 300
        cfg.normalize()

        self.tracker = Tracker(self.home / "state.json")
        self.tracker.add(source, dest_rel="2026/10/pdf/讲义.pdf", origin="scan")
        self.tracker.save()

        from archivemate.sync import SyncEngine

        self.engine = SyncEngine(cfg, self.tracker, logger=lambda _m: None)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ARCHIVEMATE_HOME", None)
        else:
            os.environ["ARCHIVEMATE_HOME"] = self._old_home
        app_paths.reset_cache()

    def test_cancel_stops_a_sync_that_is_stuck_on_the_network(self):
        import threading

        holder: dict = {}

        def worker():
            holder["outcome"] = self.engine.run()

        thread = threading.Thread(target=worker, daemon=True)
        started = time.monotonic()
        thread.start()

        # 等 git 真的跑起来（后端被建出来）
        deadline = time.monotonic() + 30
        while self.engine._backend is None and time.monotonic() < deadline:
            time.sleep(0.1)

        self.assertIsNotNone(self.engine._backend, "同步没有进入 git 调用阶段")
        time.sleep(0.5)
        self.engine.cancel()

        thread.join(timeout=60)
        self.assertFalse(thread.is_alive(), "取消之后同步线程还在跑")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 60, f"取消耗了 {elapsed:.1f}s")

        outcome = holder["outcome"]
        self.assertFalse(outcome.ok)
        self.assertIn("取消", outcome.message)
        # 文件没有被归档，也没有被算作失败
        self.assertTrue(self.downloads.joinpath("讲义.pdf").exists())
        self.assertEqual(outcome.failed, [])


if __name__ == "__main__":
    SANDBOX.mkdir(parents=True, exist_ok=True)
    unittest.main(verbosity=2)
