"""无头 GUI 测试（QT_QPA_PLATFORM=offscreen）。

这些用例不弹任何真实窗口，只验证「界面上的一次操作是否真的推动了业务逻辑」：
拖拽入队 → 队列表格刷新 → 同步 → 归档 → 清空归档。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 必须在 import PySide6 之前设置：没有显示器也要能建 QApplication
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from archivemate import app_paths  # noqa: E402
from archivemate.config import Config  # noqa: E402
from archivemate.git_backend import find_git  # noqa: E402
from archivemate.gui.theme import COLORS  # noqa: E402

COLORS_OK = COLORS["ok"]
COLORS_ERROR = COLORS["error"]

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover - 没装 PySide6 时整体跳过
    HAVE_QT = False

SANDBOX = ROOT / ".testrun"
SANDBOX.mkdir(parents=True, exist_ok=True)
GIT = find_git("")

_COUNTER = 0


def _mkdtemp(prefix: str = "gui-") -> Path:
    """不要用 tempfile.mkdtemp：它按 0o700 建目录，Windows 上会得到一份
    不能被继承的 DACL，之后在里面建子目录会报 WinError 5。"""
    global _COUNTER
    _COUNTER += 1
    path = SANDBOX / f"{prefix}{os.getpid()}-{_COUNTER}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _app() -> QApplication:
    return QApplication.instance() or QApplication(["archivemate-test"])


@unittest.skipIf(not HAVE_QT, "需要 PySide6")
@unittest.skipIf(not GIT, "需要安装 git")
class TestMainWindow(unittest.TestCase):
    """驱动真实 MainWindow 的界面级用例。"""

    def setUp(self):
        from test_archivemate import LocalOnlyBackend
        from archivemate.service import ArchiveMateService

        self.LocalOnlyBackend = LocalOnlyBackend
        self.base = _mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)

        self.home = self.base / "home"
        self.watch = self.base / "downloads"
        self.archive = self.base / "archive"
        self.remote = self.base / "remote.git"
        for directory in (self.home, self.watch):
            directory.mkdir(parents=True, exist_ok=True)
        subprocess.run([GIT, "init", "--bare", "--initial-branch", "main",
                        str(self.remote)], check=True, capture_output=True)

        self._old_home = os.environ.get("ARCHIVEMATE_HOME")
        os.environ["ARCHIVEMATE_HOME"] = str(self.home)
        app_paths.reset_cache()

        cfg = Config.default()
        cfg.watch_folders = [str(self.watch)]
        cfg.archive_folder = str(self.archive)
        cfg.repository.owner = "tester"
        cfg.repository.name = "homework"
        cfg.repository.remote_url = str(self.remote)
        cfg.repository.branch = "main"
        cfg.git.author_name = "Test Runner"
        cfg.git.author_email = "test@localhost"
        cfg.onboarding_completed = True   # 别在弹出的引导里等待
        cfg.normalize()
        self.cfg = cfg

        self.service = ArchiveMateService(
            cfg, auto_start_watcher=False,
            backend_factory=self._make_backend,
        )
        self.service.apply_config(cfg)

        _app()
        from archivemate.gui.main_window import MainWindow

        # 无头环境下任何真实的模态框都会把测试挂死。统一换成记录式假框，
        # 用例通过 self.dialogs 断言「到底弹出过什么」。
        from archivemate.gui import main_window as mw

        self.dialogs: list[tuple[str, str]] = []
        self._real_dialogs = (mw.confirm, mw.ask, mw.info, mw.warn)
        mw.confirm = self._fake_dialog("confirm", False)
        mw.ask = self._fake_dialog("ask", False)
        mw.info = self._fake_dialog("info", None)
        mw.warn = self._fake_dialog("warn", None)
        self.addCleanup(self._restore_dialogs)

        self.window = MainWindow(service=self.service)
        self.addCleanup(self._destroy_window)

    def _fake_dialog(self, kind: str, answer):
        def handler(parent, title, text, **kwargs):
            self.dialogs.append((kind, str(title)))
            return answer
        return handler

    def _restore_dialogs(self):
        from archivemate.gui import main_window as mw

        mw.confirm, mw.ask, mw.info, mw.warn = self._real_dialogs

    def dialog_titles(self, kind: str) -> list[str]:
        return [title for name, title in self.dialogs if name == kind]

    def _destroy_window(self):
        try:
            self.window._pump.stop()
            self.window._auto_timer.stop()
            self.window.service.stop()
            self.window.deleteLater()
        finally:
            if self._old_home is None:
                os.environ.pop("ARCHIVEMATE_HOME", None)
            else:
                os.environ["ARCHIVEMATE_HOME"] = self._old_home
            app_paths.reset_cache()

    def _make_backend(self):
        return self.LocalOnlyBackend(
            self.remote,
            self.cfg.repository.resolved_remote_url(),
            self.cfg.repository.branch,
            git_exe=GIT,
            author_name=self.cfg.git.author_name,
            author_email=self.cfg.git.author_email,
        )

    def _make_file(self, name: str, content: bytes = b"hello") -> Path:
        path = self.watch / name
        path.write_bytes(content)
        return path

    # ------------------------------------------------------------------

    def test_window_reflects_config(self):
        self.assertEqual(self.window.watch_editor.values(), [str(self.watch)])
        self.assertEqual(self.window.types_editor.values(),
                         list(self.cfg.file_types))
        self.assertEqual(self.window.queue_table.rowCount(), 0)
        self.assertTrue(self.window.windowTitle().startswith("ArchiveMate"))

    def test_dropped_file_lands_in_queue_and_get_archived(self):
        source = self._make_file("数学课件.pptx", b"x" * 32)

        # 模拟拖拽：走的是和真实拖放完全一样的入口
        self.window._on_files_dropped([str(source)])

        self.assertEqual(self.window.queue_table.rowCount(), 1)
        row0 = self.window.queue_table.item(0, 0)
        self.assertEqual(row0.text(), "数学课件.pptx")
        self.assertTrue(self.window.queue_table.item(0, 1).text().endswith(
            "数学课件.pptx"))
        dest = self.window.queue_table.item(0, 1).text()
        self.assertIn("课件", dest)
        self.assertIn("数学", dest)
        self.assertEqual(self.window.queue_table.item(0, 3).text(), "拖拽上传")

        outcome = self.service.sync_blocking()
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(outcome.pushed_count, 1)
        self.window._drain_events()

        # 队列已清空，文件被移动到归档目录
        self.assertEqual(self.window.queue_table.rowCount(), 0)
        self.assertFalse(source.exists())
        archived = list(self.archive.rglob("数学课件.pptx"))
        self.assertEqual(len(archived), 1)
        self.assertIn("ppt", str(archived[0].relative_to(self.archive)))
        self.assertIn("已推送 1 个", self.window.pill.text())

        # 远端 bare 仓库里确实有这份文件
        listing = subprocess.run(
            [GIT, "-c", "core.quotePath=false", "--git-dir", str(self.remote),
             "ls-tree", "-r", "--name-only", "refs/heads/main"],
            check=True, capture_output=True, encoding="utf-8")
        self.assertIn("数学课件.pptx", listing.stdout)

    def test_unsupported_extension_is_rejected_in_ui(self):
        source = self._make_file("note.txt")
        self.window._on_files_dropped([str(source)])
        self.assertEqual(self.window.queue_table.rowCount(), 0)
        self.assertIn("没有加入队列", self.window.log_view.toPlainText())

    def test_remove_selected_clears_queue_without_touching_file(self):
        source = self._make_file("作业.docx")
        self.window._on_files_dropped([str(source)])
        self.assertEqual(self.window.queue_table.rowCount(), 1)

        self.window.queue_table.selectRow(0)
        self.window._remove_selected()

        self.assertEqual(self.window.queue_table.rowCount(), 0)
        self.assertTrue(source.exists(), "移除队列不应删除本地文件")

    def test_scan_existing_picks_up_watch_folder(self):
        self._make_file("a.pdf")
        self._make_file("b.pdf")
        self._make_file("c.docx")

        self.window._scan_existing()

        self.assertEqual(self.window.queue_table.rowCount(), 3)

    def test_clear_archive_asks_then_deletes(self):
        self.window._on_files_dropped([str(self._make_file("old.pdf", b"z" * 64))])
        self.service.sync_blocking()
        files, size = self.service.archive_summary()
        self.assertEqual(files, 1)
        self.assertGreater(size, 0)

        from archivemate.gui import main_window as mw

        mw.confirm = self._fake_dialog("confirm", True)
        self.window._clear_archive()

        self.assertEqual(self.dialog_titles("confirm"), ["清空归档文件夹"])
        self.assertEqual(self.dialog_titles("info"), ["清理完成"])
        self.assertEqual(self.service.archive_summary(), (0, 0))

    def test_clear_archive_respects_cancel(self):
        self.window._on_files_dropped([str(self._make_file("keep.pdf", b"z" * 64))])
        self.service.sync_blocking()
        self.assertEqual(self.service.archive_summary()[0], 1)

        self.window._clear_archive()

        self.assertEqual(self.dialog_titles("confirm"), ["清空归档文件夹"])
        self.assertEqual(self.dialog_titles("info"), [])
        self.assertEqual(self.service.archive_summary()[0], 1)

    def test_sync_without_repository_offers_settings(self):
        self.service.config.repository.owner = ""
        self.service.config.repository.name = ""
        self.service.config.repository.remote_url = ""

        self.window._sync_now()

        self.assertEqual(self.dialog_titles("ask"), ["还不能同步"])
        self.assertFalse(self.service.syncing)

    def test_schedule_change_updates_config_and_timer(self):
        index = self.window.interval_combo.findData("daily")
        self.window.interval_combo.setCurrentIndex(index)

        self.assertEqual(self.service.config.sync_schedule, "daily")
        self.assertTrue(self.window._auto_timer.isActive())
        due = self.window._auto_due
        self.assertIsNotNone(due)
        self.assertEqual((due.hour, due.minute, due.second), (0, 0, 0))
        self.assertIn(due.strftime("%Y-%m-%d 00:00"), self.window.schedule_hint.text())

        self.window.interval_combo.setCurrentIndex(
            self.window.interval_combo.findData("off"))
        self.assertEqual(self.service.config.sync_schedule, "off")
        self.assertFalse(self.window._auto_timer.isActive())
        self.assertIsNone(self.window._auto_due)

    def test_due_schedule_triggers_a_sync_and_rearms(self):
        self.window.interval_combo.setCurrentIndex(
            self.window.interval_combo.findData("weekly"))
        # 把「到点时刻」拨到过去，模拟定时器醒来后发现该跑了
        self.window._auto_due = datetime.now() - timedelta(seconds=1)
        self.window._on_schedule_tick()

        self.assertGreater(self.window._auto_due, datetime.now())
        self.assertEqual(self.window._auto_due.weekday(), 0)
        self.assertIn(self.window._auto_due.strftime("%Y-%m-%d 00:00"),
                      self.window.schedule_hint.text())

    def test_watch_folder_edit_persists_to_disk(self):
        extra = self.base / "extra"
        extra.mkdir()
        self.window.watch_editor.add_value(str(extra))

        self.assertIn(str(extra), self.service.config.watch_folders)
        reloaded = Config.from_dict(
            __import__("json").loads(
                (self.home / "config.json").read_text(encoding="utf-8")))
        self.assertIn(str(extra), reloaded.watch_folders)

    def test_add_type_from_input_normalizes(self):
        self.window.type_input.setText("XLSX")
        self.window._add_type_from_input()
        self.assertIn(".xlsx", self.window.types_editor.values())

    # -- 取消同步 ----------------------------------------------------------

    def test_cancel_button_only_appears_while_syncing(self):
        # 用 isHidden() 而不是 isVisible()：离屏测试里窗口没有 show()，
        # isVisible() 恒为 False，看不出按钮自己的隐藏状态。
        self.assertTrue(self.window.cancel_button.isHidden())
        self.window._set_syncing(True)
        self.assertFalse(self.window.cancel_button.isHidden())
        self.assertTrue(self.window.cancel_button.isEnabled())
        self.window._set_syncing(False)
        self.assertTrue(self.window.cancel_button.isHidden())

    def test_cancel_click_disables_the_button_and_updates_the_pill(self):
        self.window._set_syncing(True)
        with mock.patch.object(self.service, "cancel_sync", return_value=True):
            self.window._cancel_sync()
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.assertEqual(self.window.pill.text(), "正在取消")

    def test_cancel_without_a_running_sync_just_logs(self):
        self.window._cancel_sync()
        self.assertIn("没有正在进行的同步", self.window.log_view.toPlainText())
        self.assertEqual(self.window.type_input.text(), "")

        # 已经存在的类型不会被重复添加，输入框也就保持原样
        self.window.type_input.setText(".pdf")
        self.window._add_type_from_input()
        self.assertEqual(self.window.types_editor.values().count(".pdf"), 1)


@unittest.skipIf(not HAVE_QT, "需要 PySide6")
class TestDialogs(unittest.TestCase):
    """对话框只做构建 + 取值回填，不 exec（否则会阻塞测试）。"""

    def setUp(self):
        self.base = _mkdtemp("dlg-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self._old_home = os.environ.get("ARCHIVEMATE_HOME")
        os.environ["ARCHIVEMATE_HOME"] = str(self.base / "home")
        (self.base / "home").mkdir(parents=True, exist_ok=True)
        app_paths.reset_cache()
        _app()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ARCHIVEMATE_HOME", None)
        else:
            os.environ["ARCHIVEMATE_HOME"] = self._old_home
        app_paths.reset_cache()

    def test_settings_dialog_roundtrip(self):
        from archivemate.gui.dialogs import SettingsDialog

        cfg = Config.default()
        dialog = SettingsDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        dialog.owner_edit.setText("alice")
        dialog.name_edit.setText("school-docs")
        dialog.branch_edit.setText("trunk")
        dialog.proxy_edit.setText("http://127.0.0.1:7890")
        dialog.warn_spin.setValue(30)
        dialog.block_spin.setValue(80)
        dialog.apply_to(cfg)

        self.assertEqual(cfg.repository.owner, "alice")
        self.assertEqual(cfg.repository.name, "school-docs")
        self.assertEqual(cfg.repository.branch, "trunk")
        self.assertEqual(cfg.git.proxy, "http://127.0.0.1:7890")
        self.assertEqual(cfg.limits.warn_bytes, 30 * 1024 * 1024)
        self.assertEqual(cfg.limits.block_bytes, 80 * 1024 * 1024)

    def test_settings_dialog_roundtrips_the_network_timeouts(self):
        from archivemate.gui.dialogs import SettingsDialog

        cfg = Config.default()
        dialog = SettingsDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        # 默认值来自配置
        self.assertEqual(dialog.network_timeout_spin.value(), 1800)
        self.assertEqual(dialog.stall_spin.value(), 60)

        dialog.network_timeout_spin.setValue(120)
        dialog.stall_spin.setValue(45)
        dialog.apply_to(cfg)

        self.assertEqual(cfg.git.resolved_network_timeout(), 120)
        self.assertEqual(cfg.git.resolved_stall_timeout(), 45)

    def test_stall_spin_accepts_zero_as_off(self):
        from archivemate.gui.dialogs import SettingsDialog

        cfg = Config.default()
        dialog = SettingsDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        dialog.stall_spin.setValue(0)
        dialog.apply_to(cfg)
        self.assertEqual(cfg.git.resolved_stall_timeout(), 0)
        self.assertEqual(dialog.stall_spin.text(), "关闭")

    def test_onboarding_dialog_marks_completed(self):
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        cfg.onboarding_completed = False
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        dialog.apply_to(cfg)
        self.assertTrue(cfg.onboarding_completed)

    def test_onboarding_dialog_collects_types_and_folders(self):
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        cfg.onboarding_completed = False
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        dialog.watch_edit.setText(r"D:\school\downloads")
        dialog.archive_edit.setText(r"D:\school\archive")
        dialog.types_edit.setText(".pdf, XLSX .pptx")
        dialog.apply_to(cfg)

        self.assertEqual(cfg.watch_folders, [r"D:\school\downloads"])
        self.assertEqual(cfg.archive_folder, r"D:\school\archive")
        self.assertEqual(cfg.file_types, [".pdf", ".xlsx", ".pptx"])

    def test_settings_dialog_roundtrips_ssl_backend(self):
        from archivemate.gui.dialogs import SettingsDialog

        cfg = Config.default()
        dialog = SettingsDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        index = dialog.ssl_combo.findData("openssl")
        self.assertGreaterEqual(index, 0, "高级页签里应该有 OpenSSL 选项")
        dialog.ssl_combo.setCurrentIndex(index)
        dialog.apply_to(cfg)
        self.assertEqual(cfg.git.ssl_backend, "openssl")

        # 换回「跟随系统」应该清空
        dialog.ssl_combo.setCurrentIndex(dialog.ssl_combo.findData(""))
        dialog.apply_to(cfg)
        self.assertEqual(cfg.git.ssl_backend, "")


class TestTokenHelpers(unittest.TestCase):
    """Token 相关的三个便利功能：一键建 Token、自动校验、用户名回填。"""

    def setUp(self):
        self.base = _mkdtemp("tok-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self._old_home = os.environ.get("ARCHIVEMATE_HOME")
        os.environ["ARCHIVEMATE_HOME"] = str(self.base / "home")
        (self.base / "home").mkdir(parents=True, exist_ok=True)
        app_paths.reset_cache()
        _app()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ARCHIVEMATE_HOME", None)
        else:
            os.environ["ARCHIVEMATE_HOME"] = self._old_home
        app_paths.reset_cache()

    # -- 一键新建 Token -------------------------------------------------

    def test_token_url_points_at_github_token_page(self):
        from archivemate.gui import dialogs

        self.assertEqual(dialogs.TOKEN_CREATE_URL,
                         "https://github.com/settings/tokens/new")

    def test_onboarding_has_a_token_page_button(self):
        from archivemate.gui import dialogs

        cfg = Config.default()
        dialog = dialogs.OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        self.assertIn("Token", dialog.get_token_button.text())
        with mock.patch.object(dialogs, "open_token_page",
                               return_value=True) as opener:
            dialog.get_token_button.click()
        opener.assert_called_once_with()

    def test_settings_has_a_token_page_button(self):
        from archivemate.gui import dialogs

        cfg = Config.default()
        dialog = dialogs.SettingsDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        with mock.patch.object(dialogs, "open_token_page",
                               return_value=False) as opener:
            dialog.get_token_button.click()
        opener.assert_called_once_with()
        # 浏览器打不开时必须给出可以手抄的地址，不能静默失败
        self.assertIn(dialogs.TOKEN_CREATE_URL, dialog.token_status.text())

    # -- 自动校验与用户名回填 -------------------------------------------

    def test_probe_success_fills_in_the_username(self):
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        cfg.onboarding_completed = False
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.owner_edit.text(), "")

        dialog._on_probe_done(True, "连接成功，已登录为 alice", "alice")

        self.assertEqual(dialog.owner_edit.text(), "alice")
        self.assertIn("✓", dialog.token_status.text())
        self.assertFalse(dialog.token_status.isHidden())
        self.assertIn(COLORS_OK, dialog.token_status.styleSheet())

    def test_probe_failure_is_red_and_leaves_username_alone(self):
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        dialog._on_probe_done(False, "Token 无效或已过期，请重新填写。", "")

        self.assertIn("✗", dialog.token_status.text())
        self.assertIn("Token 无效", dialog.token_status.text())
        self.assertIn(COLORS_ERROR, dialog.token_status.styleSheet())
        self.assertFalse(dialog.token_status.isHidden())
        self.assertEqual(dialog.owner_edit.text(), "")

    def test_probe_does_not_overwrite_a_hand_typed_owner(self):
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        dialog.owner_edit.setText("my-org")

        dialog._on_probe_done(True, "连接成功，已登录为 alice", "alice")

        self.assertEqual(dialog.owner_edit.text(), "my-org")

    def test_autofilled_owner_can_be_replaced_by_a_later_probe(self):
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        dialog._on_probe_done(True, "ok", "alice")
        dialog.token_edit.setText("ghp_second")
        dialog._on_probe_done(True, "ok", "bob")

        self.assertEqual(dialog.owner_edit.text(), "bob")

    def test_typing_a_token_triggers_a_probe_once(self):
        from archivemate.gui import dialogs

        cfg = Config.default()
        dialog = dialogs.OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        token = "ghp_" + "a" * 36

        with mock.patch.object(dialogs, "_ApiProbe") as probe_cls:
            dialog.token_edit.setText(token)
            dialog._auto_test_token()
            dialog._auto_test_token()      # 失焦两次不应该发两个请求

        probe_cls.assert_called_once()
        self.assertEqual(probe_cls.call_args[0][0], token)

    def test_short_input_does_not_hit_the_network(self):
        from archivemate.gui import dialogs

        cfg = Config.default()
        dialog = dialogs.OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)

        with mock.patch.object(dialogs, "_ApiProbe") as probe_cls:
            dialog.token_edit.setText("oops")
            dialog._auto_test_token()

        probe_cls.assert_not_called()

    # -- 仓库名兜底 -----------------------------------------------------

    def test_blank_repository_name_falls_back_in_onboarding(self):
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        dialog.owner_edit.setText("alice")
        dialog.name_edit.setText("   ")

        dialog.apply_to(cfg)

        self.assertEqual(cfg.repository.name, "school-files")
        self.assertEqual(cfg.repository.resolved_remote_url(),
                         "https://github.com/alice/school-files.git")

    def test_blank_repository_name_falls_back_in_settings(self):
        from archivemate.gui.dialogs import SettingsDialog

        cfg = Config.default()
        dialog = SettingsDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        dialog.owner_edit.setText("alice")
        dialog.name_edit.clear()

        dialog.apply_to(cfg)

        self.assertEqual(cfg.repository.name, "school-files")

    def test_create_repository_uses_the_default_name(self):
        import archivemate.github_api as api
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        dialog.token_edit.setText("ghp_fake")
        dialog.name_edit.clear()

        info = api.RepositoryInfo(owner="alice", name="school-files", private=True,
                                  default_branch="main", clone_url="")
        with mock.patch.object(api, "GitHubClient", autospec=True) as client_cls:
            client_cls.return_value.create_repository.return_value = info
            dialog._create_repository()

        client_cls.return_value.create_repository.assert_called_once()
        self.assertEqual(
            client_cls.return_value.create_repository.call_args[0][0],
            "school-files")


class _StubDownloader:
    """假下载器：列目录返回给定文件，下载时按顺序回调进度。"""

    def __init__(self, files, payload=b"data") -> None:
        self.files = list(files)
        self.payload = payload
        self.cancelled = False
        self.downloaded: list = []
        self.fail: Exception | None = None

    def list_files(self):
        return list(self.files)

    def download_many(self, files, dest_dir):
        from archivemate.errors import DownloadCancelled

        saved = []
        for index, item in enumerate(files, start=1):
            if self.cancelled:
                raise DownloadCancelled("已取消下载。")
            if self.fail is not None:
                raise self.fail
            target = Path(dest_dir) / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.payload)
            saved.append(target)
            self.downloaded.append(item)
            if self.on_progress is not None:
                total = len(self.payload)
                self.on_progress(item.path, total, total)
        return saved

    on_progress = None

    def cancel(self):
        self.cancelled = True


class _StubService:
    def __init__(self, folder: Path, downloader) -> None:
        self.folder = folder
        self.downloader = downloader

    def download_folder(self) -> Path:
        return self.folder

    def build_downloader(self, **_kwargs):
        return self.downloader


@unittest.skipIf(not HAVE_QT, "需要 PySide6")
class TestDownloadDialog(unittest.TestCase):
    """下载对话框：列文件、勾选、进度、完成/取消文案。"""

    def setUp(self):
        from archivemate.downloader import RemoteFile

        _app()
        self.RemoteFile = RemoteFile
        self.base = _mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.target = self.base / "downloads"
        self.target.mkdir(parents=True, exist_ok=True)
        self.files = [
            RemoteFile("2026-2027学年/上学期/课件/数学/数学课件.pptx", 2048),
            RemoteFile("2026-2027学年/上学期/其他/语文/作文模板.docx", 512),
        ]

    def _dialog(self, downloader=None, folder=None):
        from archivemate.gui.dialogs import DownloadDialog

        stub = downloader or _StubDownloader(self.files)
        service = _StubService(folder or self.target, stub)
        dialog = DownloadDialog(service)
        self.addCleanup(dialog.deleteLater)
        if dialog._list_thread is not None:
            dialog._list_thread.wait(5000)
        _app().processEvents()
        return dialog, stub

    def test_lists_remote_files_with_sizes(self):
        dialog, _stub = self._dialog()
        self.assertEqual(dialog.list_widget.count(), 2)
        text = dialog.list_widget.item(0).text()
        self.assertIn("数学课件.pptx", text)
        self.assertIn("2.0 KB", text)
        self.assertIn(str(self.target), dialog.target_label.text())

    def test_nothing_selected_by_default(self):
        dialog, _stub = self._dialog()
        self.assertFalse(dialog.download_button.isEnabled())
        self.assertEqual(dialog._checked(), [])

    def test_select_all_enables_download(self):
        dialog, _stub = self._dialog()
        dialog._set_all(True)
        self.assertTrue(dialog.download_button.isEnabled())
        self.assertEqual(len(dialog._checked()), 2)
        dialog._set_all(False)
        self.assertFalse(dialog.download_button.isEnabled())

    def test_download_checked_writes_files_and_reports(self):
        dialog, stub = self._dialog()
        dialog._set_all(True)
        dialog._download_checked()
        if dialog._worker is not None:
            dialog._worker.wait(5000)
        _app().processEvents()

        self.assertEqual(len(stub.downloaded), 2)
        self.assertTrue((self.target / "数学课件.pptx").is_file())
        self.assertIn("✓", dialog.hint.text())
        self.assertFalse(dialog.progress.isVisible())

    def test_download_all_takes_everything(self):
        dialog, stub = self._dialog()
        dialog._download_all()
        if dialog._worker is not None:
            dialog._worker.wait(5000)
        _app().processEvents()
        self.assertEqual(len(stub.downloaded), 2)

    def test_progress_updates_hint(self):
        dialog, _stub = self._dialog()
        dialog._on_progress("a/b/c.pptx", 512, 2048)
        self.assertEqual(dialog.progress.value(), 25)
        self.assertIn("c.pptx", dialog.hint.text())
        self.assertIn("512 B", dialog.hint.text())

    def test_progress_without_total_shows_bytes(self):
        dialog, _stub = self._dialog()
        dialog._on_progress("a/b/c.pptx", 4096, 0)
        self.assertIn("4.0 KB", dialog.hint.text())

    def test_cancel_text_is_shown_warm(self):
        dialog, _stub = self._dialog()
        dialog._on_finished(False, [], "已取消下载。")
        self.assertIn("取消", dialog.hint.text())
        self.assertIn(COLORS["warn"], dialog.hint.styleSheet())

    def test_failure_text_is_shown_red(self):
        dialog, _stub = self._dialog()
        dialog._on_finished(False, [], "网络断了")
        self.assertIn("网络断了", dialog.hint.text())
        self.assertIn(COLORS_ERROR, dialog.hint.styleSheet())

    def test_list_failure_is_shown_red(self):
        dialog, _stub = self._dialog(downloader=_StubDownloader(self.files))
        dialog._on_listed(False, [], "Token 无效或已过期")
        self.assertIn("Token 无效", dialog.hint.text())
        self.assertIn(COLORS_ERROR, dialog.hint.styleSheet())
        self.assertEqual(dialog.list_widget.count(), 0)

    def test_empty_repository_message(self):
        dialog, _stub = self._dialog(downloader=_StubDownloader([]))
        self.assertEqual(dialog.list_widget.count(), 0)
        self.assertIn("还没有文件", dialog.hint.text())

    def test_download_failure_surfaces_message(self):
        from archivemate.errors import DownloadError

        stub = _StubDownloader(self.files)
        stub.fail = DownloadError("下载停滞超过 120 秒，已中止。")
        dialog, _stub = self._dialog(downloader=stub)
        dialog._download_all()
        if dialog._worker is not None:
            dialog._worker.wait(5000)
        _app().processEvents()
        self.assertIn("停滞", dialog.hint.text())
        self.assertIn(COLORS_ERROR, dialog.hint.styleSheet())

    def test_cancel_button_clears_worker(self):
        dialog, stub = self._dialog()
        dialog._set_all(True)
        dialog._download_all()
        if dialog._worker is not None:
            dialog._worker.wait(5000)
        _app().processEvents()
        self.assertIsNone(dialog._worker)
        self.assertFalse(dialog.cancel_button.isVisible())


@unittest.skipIf(not HAVE_QT, "需要 PySide6")
@unittest.skipIf(not GIT, "需要安装 git")
class TestDownloadButtonWiring(unittest.TestCase):
    """主界面的「下载文件」按钮：配置不全时给提示，别静默失败。"""

    def setUp(self):
        from test_archivemate import LocalOnlyBackend
        from archivemate.service import ArchiveMateService
        from archivemate.gui import main_window as mw

        self.LocalOnlyBackend = LocalOnlyBackend
        self.base = _mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / "home"
        self.watch = self.base / "watch"
        for directory in (self.home, self.watch):
            directory.mkdir(parents=True, exist_ok=True)
        self._old_home = os.environ.get("ARCHIVEMATE_HOME")
        os.environ["ARCHIVEMATE_HOME"] = str(self.home)
        app_paths.reset_cache()

        cfg = Config.default()
        cfg.watch_folders = [str(self.watch)]
        cfg.archive_folder = str(self.base / "archive")
        cfg.onboarding_completed = True
        cfg.normalize()

        self.service = ArchiveMateService(
            cfg, auto_start_watcher=False,
            backend_factory=lambda: LocalOnlyBackend(
                self.base / "remote.git", "", "main", git_exe=GIT))
        self.service.apply_config(cfg)

        _app()
        self.dialogs: list[tuple[str, str]] = []

        def handler(parent, title, text, **kwargs):
            self.dialogs.append((str(title), str(text)))
            return None

        self._real = (mw.warn, mw.info, mw.confirm, mw.ask)
        mw.warn = mw.info = mw.confirm = mw.ask = handler
        self.addCleanup(self._restore)
        self.window = mw.MainWindow(service=self.service)
        self.addCleanup(self._destroy)

    def _restore(self):
        from archivemate.gui import main_window as mw

        mw.warn, mw.info, mw.confirm, mw.ask = self._real

    def _destroy(self):
        try:
            self.window._pump.stop()
            self.window._auto_timer.stop()
            self.window.service.stop()
            self.window.deleteLater()
        finally:
            if self._old_home is None:
                os.environ.pop("ARCHIVEMATE_HOME", None)
            else:
                os.environ["ARCHIVEMATE_HOME"] = self._old_home
            app_paths.reset_cache()

    def test_button_exists(self):
        self.assertEqual(self.window.download_button.text(), "下载文件")

    def test_warns_when_repository_missing(self):
        self.window.service.config.repository.owner = ""
        self.window.service.config.repository.name = ""
        self.window._open_download()
        self.assertTrue(any("仓库" in title for title, _text in self.dialogs))

    def test_disabled_while_syncing(self):
        self.window._set_syncing(True)
        self.assertFalse(self.window.download_button.isEnabled())
        self.window._set_syncing(False)
        self.assertTrue(self.window.download_button.isEnabled())


class TestGitHubApiCallSites(unittest.TestCase):
    """界面里调用 GitHub 客户端的地方，参数必须真的对得上。

    回归来源：真实用户点「创建私有仓库」时报
    `GitHubClient.__init__() takes 2 positional arguments but 3 were given`
    —— `proxy` 是 keyword-only，而两处调用都按位置传了。
    用 autospec 让 mock 也校验签名，这类错误才拦得住。
    """

    def setUp(self):
        self.base = _mkdtemp("api-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self._old_home = os.environ.get("ARCHIVEMATE_HOME")
        os.environ["ARCHIVEMATE_HOME"] = str(self.base / "home")
        (self.base / "home").mkdir(parents=True, exist_ok=True)
        app_paths.reset_cache()
        _app()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ARCHIVEMATE_HOME", None)
        else:
            os.environ["ARCHIVEMATE_HOME"] = self._old_home
        app_paths.reset_cache()

    def test_create_repository_button_calls_the_client_correctly(self):
        import archivemate.github_api as api
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        cfg.git.proxy = "http://127.0.0.1:7890"
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        dialog.token_edit.setText("ghp_fake")
        dialog.name_edit.setText("school-files")

        info = api.RepositoryInfo(owner="alice", name="school-files", private=True,
                                  default_branch="main", clone_url="")
        with mock.patch.object(api, "GitHubClient", autospec=True) as client_cls:
            client_cls.return_value.create_repository.return_value = info
            dialog._create_repository()

        self.assertTrue(client_cls.called, "没有调用 GitHub 客户端")
        # mock 的 autospec 会校验签名，参数传错在这里就会 TypeError
        self.assertEqual(dialog.owner_edit.text(), "alice")
        self.assertIn("school-files", dialog.repo_status.text())
        self.assertNotIn("✗", dialog.repo_status.text())

    def test_api_probe_calls_the_client_correctly(self):
        import archivemate.github_api as api
        from archivemate.gui.dialogs import _ApiProbe

        probe = _ApiProbe("ghp_fake", "http://127.0.0.1:7890")
        self.addCleanup(probe.deleteLater)

        with mock.patch.object(api, "GitHubClient", autospec=True) as client_cls:
            client_cls.return_value.get_user.return_value = api.UserInfo(
                login="alice", name="Alice")
            probe.run()          # 同步跑，不进事件循环

        self.assertTrue(client_cls.called, "没有调用 GitHub 客户端")

    def test_create_repository_failure_is_shown_not_raised(self):
        import archivemate.github_api as api
        from archivemate.errors import GitHubError
        from archivemate.gui.dialogs import OnboardingDialog

        cfg = Config.default()
        dialog = OnboardingDialog(cfg)
        self.addCleanup(dialog.deleteLater)
        dialog.token_edit.setText("ghp_fake")
        dialog.name_edit.setText("school-files")

        with mock.patch.object(api, "GitHubClient", autospec=True) as client_cls:
            client_cls.return_value.create_repository.side_effect = GitHubError(
                "Token 无效或已过期，请重新填写。", status=401)
            dialog._create_repository()      # 不能把异常抛给 Qt 事件循环

        self.assertIn("✗", dialog.repo_status.text())
        # 对话框本身没 show()，所以只能查 isHidden()（isVisible 会因父窗口隐藏而为假）
        self.assertFalse(dialog.repo_status.isHidden(),
                         "失败信息应该显示出来，而不是留一条空标签")


if __name__ == "__main__":
    unittest.main()
