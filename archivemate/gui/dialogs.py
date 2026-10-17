"""设置对话框、首次引导与仓库选择。"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .. import CREDENTIAL_TARGET, credentials
from ..config import SYNC_INTERVAL_CHOICES, Config, normalize_extension
from ..fsutil import MiB, human_size
from .theme import COLORS


class _ApiProbe(QThread):
    """在后台线程里验证 Token 是否可用，避免卡住界面。"""

    finished_probe = Signal(bool, str)

    def __init__(self, token: str, proxy: str, parent=None) -> None:
        super().__init__(parent)
        self.token = token
        self.proxy = proxy

    def run(self) -> None:  # noqa: D102
        try:
            from ..github_api import GitHubClient

            user = GitHubClient(self.token, proxy=self.proxy, timeout=20).get_user()
            self.finished_probe.emit(True, f"连接成功，已登录为 {user.login}")
        except Exception as exc:  # 网络/权限问题都统一报给用户
            self.finished_probe.emit(False, str(exc))


class SettingsDialog(QDialog):
    """仓库、Token、归档、同步、网络等所有可配置项。"""

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)

        self._config = config.copy()
        self._probe: _ApiProbe | None = None
        try:
            self._token = credentials.get_token(CREDENTIAL_TARGET) or ""
        except Exception:
            self._token = ""

        tabs = QTabWidget(self)
        tabs.addTab(self._build_account_tab(), "仓库与账号")
        tabs.addTab(self._build_flow_tab(), "归档与同步")
        tabs.addTab(self._build_advanced_tab(), "高级")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # 页签
    # ------------------------------------------------------------------

    def _build_account_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)

        repo_box = QGroupBox("GitHub 私有仓库", page)
        repo_form = QFormLayout(repo_box)
        self.owner_edit = QLineEdit(self._config.repository.owner, repo_box)
        self.owner_edit.setPlaceholderText("用户名或组织名，例如 zhangsan")
        self.name_edit = QLineEdit(self._config.repository.name, repo_box)
        self.name_edit.setPlaceholderText("仓库名，例如 school-files")
        self.branch_edit = QLineEdit(self._config.repository.branch or "main", repo_box)
        self.remote_edit = QLineEdit(self._config.repository.remote_url, repo_box)
        self.remote_edit.setPlaceholderText("留空则自动使用 https://github.com/<用户名>/<仓库名>.git")
        repo_form.addRow("用户名", self.owner_edit)
        repo_form.addRow("仓库名", self.name_edit)
        repo_form.addRow("分支", self.branch_edit)
        repo_form.addRow("自定义远端", self.remote_edit)

        token_box = QGroupBox("访问令牌（Token）", page)
        token_layout = QVBoxLayout(token_box)
        row = QHBoxLayout()
        self.token_edit = QLineEdit(token_box)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setText(self._token)
        self.token_edit.setPlaceholderText("粘贴一个有 repo 权限的 Token")
        self.show_token = QCheckBox("显示", token_box)
        self.show_token.toggled.connect(
            lambda on: self.token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row.addWidget(self.token_edit, 1)
        row.addWidget(self.show_token)
        token_layout.addLayout(row)

        actions = QHBoxLayout()
        self.test_button = QPushButton("测试连接", token_box)
        self.test_button.clicked.connect(self._test_token)
        clear_button = QPushButton("清除已保存的 Token", token_box)
        clear_button.clicked.connect(self._clear_token)
        actions.addWidget(self.test_button)
        actions.addWidget(clear_button)
        actions.addStretch(1)
        token_layout.addLayout(actions)

        self.token_status = QLabel("", token_box)
        self.token_status.setObjectName("hint")
        self.token_status.setWordWrap(True)
        token_layout.addWidget(self.token_status)
        hint = QLabel(credentials.storage_hint(), token_box)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        token_layout.addWidget(hint)
        if not credentials.is_available():
            warn = QLabel("⚠ 当前系统密钥链不可用，Token 只在本次运行期间有效，"
                          "关闭软件后需要重新输入。", token_box)
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {COLORS['warn']};")
            token_layout.addWidget(warn)

        outer.addWidget(repo_box)
        outer.addWidget(token_box)
        outer.addStretch(1)
        return page

    def _build_flow_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)

        sync_box = QGroupBox("同步", page)
        sync_form = QFormLayout(sync_box)
        self.interval_combo = QComboBox(sync_box)
        for minutes, label in SYNC_INTERVAL_CHOICES:
            self.interval_combo.addItem(label, minutes)
        index = self.interval_combo.findData(self._config.sync_interval_minutes)
        self.interval_combo.setCurrentIndex(index if index >= 0 else 0)
        self.date_combo = QComboBox(sync_box)
        self.date_combo.addItem("按文件修改日期（推荐）", "mtime")
        self.date_combo.addItem("按归档当天日期", "now")
        date_index = self.date_combo.findData(self._config.date_source)
        self.date_combo.setCurrentIndex(date_index if date_index >= 0 else 0)
        sync_form.addRow("定时同步", self.interval_combo)
        sync_form.addRow("分类日期", self.date_combo)

        archive_box = QGroupBox("归档文件夹", page)
        archive_layout = QVBoxLayout(archive_box)
        row = QHBoxLayout()
        self.archive_edit = QLineEdit(self._config.archive_folder, archive_box)
        browse = QPushButton("选择…", archive_box)
        browse.clicked.connect(self._pick_archive)
        row.addWidget(self.archive_edit, 1)
        row.addWidget(browse)
        archive_layout.addLayout(row)
        archive_hint = QLabel("文件推送成功后会被移动到这里的 年/月/类型/ 目录下，"
                              "而不是直接删除。", archive_box)
        archive_hint.setObjectName("hint")
        archive_hint.setWordWrap(True)
        archive_layout.addWidget(archive_hint)

        outer.addWidget(sync_box)
        outer.addWidget(archive_box)
        outer.addStretch(1)
        return page

    def _build_advanced_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)

        net_box = QGroupBox("网络", page)
        net_form = QFormLayout(net_box)
        self.proxy_edit = QLineEdit(self._config.git.proxy, net_box)
        self.proxy_edit.setPlaceholderText("例如 http://127.0.0.1:7890，留空为直连")
        net_form.addRow("代理", self.proxy_edit)

        self.ssl_combo = QComboBox(net_box)
        self.ssl_combo.addItem("跟随系统（推荐）", "")
        self.ssl_combo.addItem("系统 schannel", "schannel")
        self.ssl_combo.addItem("OpenSSL", "openssl")
        current = self._config.git.resolved_ssl_backend()
        self.ssl_combo.setCurrentIndex(max(0, self.ssl_combo.findData(current)))
        self.ssl_combo.setToolTip(
            "推送报「证书校验失败」或「schannel 拿不到凭据」时，换一个再试。")
        net_form.addRow("HTTPS 证书后端", self.ssl_combo)
        ssl_hint = QLabel(
            "只有 HTTPS 连接报证书错误时才需要改这里；换完记得重新同步一次。",
            net_box)
        ssl_hint.setObjectName("hint")
        ssl_hint.setWordWrap(True)
        net_form.addRow("", ssl_hint)

        self.network_timeout_spin = QSpinBox(net_box)
        self.network_timeout_spin.setRange(30, 3600)
        self.network_timeout_spin.setSuffix(" 秒")
        self.network_timeout_spin.setValue(self._config.git.resolved_network_timeout())
        self.network_timeout_spin.setToolTip(
            "一次网络操作最多等多久。超过就中断并提示，不会一直卡住。")
        net_form.addRow("等待上限", self.network_timeout_spin)

        self.stall_spin = QSpinBox(net_box)
        self.stall_spin.setRange(0, 600)
        self.stall_spin.setSuffix(" 秒")
        self.stall_spin.setSpecialValueText("关闭")
        self.stall_spin.setValue(self._config.git.resolved_stall_timeout())
        self.stall_spin.setToolTip(
            "连接建立后多久收不到数据就放弃。0 表示关闭这条保护（不建议）。")
        net_form.addRow("卡住判定", self.stall_spin)

        timeout_hint = QLabel(
            "网络不好时，任务会在「等待上限」到点后自动结束并提示，"
            "不会像以前那样一直卡在「正在同步」；同步中也可以点「取消同步」。",
            net_box)
        timeout_hint.setObjectName("hint")
        timeout_hint.setWordWrap(True)
        net_form.addRow("", timeout_hint)

        git_box = QGroupBox("git", page)
        git_form = QFormLayout(git_box)
        row = QHBoxLayout()
        self.git_edit = QLineEdit(self._config.git.executable, git_box)
        self.git_edit.setPlaceholderText("留空则自动从 PATH 查找")
        git_browse = QPushButton("选择…", git_box)
        git_browse.clicked.connect(self._pick_git)
        row.addWidget(self.git_edit, 1)
        row.addWidget(git_browse)
        git_form.addRow("git 路径", row)

        limit_box = QGroupBox("大小限制", page)
        limit_form = QFormLayout(limit_box)
        self.warn_spin = QSpinBox(limit_box)
        self.warn_spin.setRange(1, 4096)
        self.warn_spin.setSuffix(" MiB")
        self.warn_spin.setValue(int(self._config.limits.warn_bytes // MiB))
        self.block_spin = QSpinBox(limit_box)
        self.block_spin.setRange(1, 4096)
        self.block_spin.setSuffix(" MiB")
        self.block_spin.setValue(int(self._config.limits.block_bytes // MiB))
        limit_form.addRow("超过则提醒", self.warn_spin)
        limit_form.addRow("超过则拒绝推送", self.block_spin)

        outer.addWidget(net_box)
        outer.addWidget(git_box)
        outer.addWidget(limit_box)
        outer.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _pick_archive(self) -> None:
        start = self.archive_edit.text().strip() or ""
        chosen = QFileDialog.getExistingDirectory(self, "选择归档文件夹", start)
        if chosen:
            self.archive_edit.setText(chosen)

    def _pick_git(self) -> None:
        filters = "git 可执行文件 (git.exe git);;所有文件 (*)"
        chosen, _ = QFileDialog.getOpenFileName(self, "选择 git", "", filters)
        if chosen:
            self.git_edit.setText(chosen)

    def _clear_token(self) -> None:
        try:
            credentials.delete_token(CREDENTIAL_TARGET)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", f"无法从系统密钥链删除 Token：{exc}")
            return
        credentials.remember_for_session("", CREDENTIAL_TARGET)
        self._token = ""
        self.token_edit.clear()
        self.token_status.setText("已删除保存的 Token。")

    def _test_token(self) -> None:
        token = self.token_edit.text().strip()
        if not token:
            self.token_status.setText("请先粘贴 Token。")
            return
        self.test_button.setEnabled(False)
        self.token_status.setText("正在连接 GitHub……")
        self._probe = _ApiProbe(token, self.proxy_edit.text().strip(), self)
        self._probe.finished_probe.connect(self._on_probe_done)
        self._probe.start()

    def _on_probe_done(self, ok: bool, message: str) -> None:
        self.test_button.setEnabled(True)
        color = COLORS["ok"] if ok else COLORS["error"]
        self.token_status.setText(("✓ " if ok else "✗ ") + message)
        self.token_status.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------------
    # 结果
    # ------------------------------------------------------------------

    def _on_accept(self) -> None:
        token = self.token_edit.text().strip()
        if token != self._token:
            if token:
                try:
                    credentials.set_token(token, CREDENTIAL_TARGET)
                except Exception:
                    # 密钥链写不进去也不能丢：本次运行内继续可用
                    credentials.remember_for_session(token, CREDENTIAL_TARGET)
                    QMessageBox.information(
                        self, "无法写入系统密钥链",
                        "Token 无法保存到系统密钥链，本次运行仍然可用，"
                        "但重启软件后需要重新输入。")
            else:
                try:
                    credentials.delete_token(CREDENTIAL_TARGET)
                except Exception:
                    pass
                credentials.remember_for_session("", CREDENTIAL_TARGET)
            self._token = token
        self.accept()

    def apply_to(self, config: Config) -> Config:
        """把对话框里的内容写回配置对象。"""
        repo = config.repository
        repo.owner = self.owner_edit.text().strip()
        repo.name = self.name_edit.text().strip()
        repo.branch = self.branch_edit.text().strip() or "main"
        repo.remote_url = self.remote_edit.text().strip()

        config.archive_folder = self.archive_edit.text().strip()
        config.sync_interval_minutes = int(self.interval_combo.currentData())
        config.date_source = str(self.date_combo.currentData())
        config.git.proxy = self.proxy_edit.text().strip()
        config.git.ssl_backend = str(self.ssl_combo.currentData() or "")
        config.git.executable = self.git_edit.text().strip()
        config.git.network_timeout_seconds = int(self.network_timeout_spin.value())
        config.git.stall_timeout_seconds = int(self.stall_spin.value())

        warn = int(self.warn_spin.value()) * MiB
        block = int(self.block_spin.value()) * MiB
        if warn > block:
            warn = block
        config.limits.warn_bytes = warn
        config.limits.block_bytes = block
        config.normalize()
        return config


class OnboardingDialog(QDialog):
    """首次使用引导：填 Token → 选仓库 → 选监控文件夹与类型 → 选归档目录。

    不做成强制流程，取消就等于跳过，之后可以在设置里改。
    """

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("欢迎使用 ArchiveMate 归巢")
        self.setMinimumWidth(540)

        self._config = config.copy()
        try:
            self._token = credentials.get_token(CREDENTIAL_TARGET) or ""
        except Exception:
            self._token = ""

        intro = QLabel(
            "三步就能用起来：\n"
            "1. 粘贴一个 GitHub Token（需要 repo 权限）\n"
            "2. 填写要归档到的私有仓库\n"
            "3. 确认要监控的文件夹、目标文件类型和归档位置",
            self)
        intro.setWordWrap(True)

        token_box = QGroupBox("1. GitHub Token", self)
        token_layout = QVBoxLayout(token_box)
        self.token_edit = QLineEdit(self._token, token_box)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("ghp_… 或 github_pat_…")
        token_layout.addWidget(self.token_edit)
        token_hint = QLabel(credentials.storage_hint(), token_box)
        token_hint.setObjectName("hint")
        token_layout.addWidget(token_hint)

        repo_box = QGroupBox("2. 归档仓库", self)
        repo_form = QFormLayout(repo_box)
        self.owner_edit = QLineEdit(self._config.repository.owner, repo_box)
        self.owner_edit.setPlaceholderText("GitHub 用户名")
        self.name_edit = QLineEdit(self._config.repository.name, repo_box)
        self.name_edit.setPlaceholderText("例如 school-files（需为私有仓库）")
        repo_form.addRow("用户名", self.owner_edit)
        repo_form.addRow("仓库名", self.name_edit)
        create_row = QHBoxLayout()
        self.create_button = QPushButton("创建私有仓库", repo_box)
        self.create_button.clicked.connect(self._create_repository)
        create_row.addWidget(self.create_button)
        create_row.addStretch(1)
        repo_form.addRow("", create_row)
        self.repo_status = QLabel("", repo_box)
        self.repo_status.setObjectName("hint")
        self.repo_status.setWordWrap(True)
        self.repo_status.setVisible(False)
        repo_form.addRow("", self.repo_status)

        folder_box = QGroupBox("3. 本地设置", self)
        folder_form = QFormLayout(folder_box)
        watch_row = QHBoxLayout()
        self.watch_edit = QLineEdit(
            self._config.watch_folders[0] if self._config.watch_folders else "",
            folder_box)
        watch_browse = QPushButton("选择…", folder_box)
        watch_browse.clicked.connect(self._pick_watch)
        watch_row.addWidget(self.watch_edit, 1)
        watch_row.addWidget(watch_browse)
        archive_row = QHBoxLayout()
        self.archive_edit = QLineEdit(self._config.archive_folder, folder_box)
        archive_browse = QPushButton("选择…", folder_box)
        archive_browse.clicked.connect(self._pick_archive)
        archive_row.addWidget(self.archive_edit, 1)
        archive_row.addWidget(archive_browse)
        folder_form.addRow("监控文件夹", watch_row)
        self.types_edit = QLineEdit(" ".join(self._config.file_types), folder_box)
        self.types_edit.setPlaceholderText(".pdf .pptx .docx")
        folder_form.addRow("目标文件类型", self.types_edit)
        folder_form.addRow("归档文件夹", archive_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始使用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("跳过")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(token_box)
        layout.addWidget(repo_box)
        layout.addWidget(folder_box)
        layout.addWidget(buttons)

    # -- 交互 ---------------------------------------------------------

    def _pick_watch(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择要监控的文件夹",
                                                  self.watch_edit.text().strip())
        if chosen:
            self.watch_edit.setText(chosen)

    def _pick_archive(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择归档文件夹",
                                                  self.archive_edit.text().strip())
        if chosen:
            self.archive_edit.setText(chosen)

    def _set_status(self, text: str, ok: bool) -> None:
        self.repo_status.setText(text)
        self.repo_status.setVisible(bool(text))
        self.repo_status.setStyleSheet(
            f"color: {COLORS['ok'] if ok else COLORS['error']};")

    def _create_repository(self) -> None:
        token = self.token_edit.text().strip()
        name = self.name_edit.text().strip()
        if not token or not name:
            self._set_status("请先填写 Token 和仓库名。", False)
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from ..github_api import GitHubClient

            client = GitHubClient(token, proxy=self._config.git.proxy)
            info = client.create_repository(name, private=True,
                                            description="ArchiveMate 归档的学校资料")
            self.owner_edit.setText(info.owner or self.owner_edit.text())
            self.name_edit.setText(info.name or name)
            self._set_status(f"✓ 已创建私有仓库 {info.owner}/{info.name}", True)
        except Exception as exc:
            self._set_status(f"✗ {exc}", False)
        finally:
            QApplication.restoreOverrideCursor()

    def _on_accept(self) -> None:
        token = self.token_edit.text().strip()
        if token and token != self._token:
            try:
                credentials.set_token(token, CREDENTIAL_TARGET)
            except Exception:
                credentials.remember_for_session(token, CREDENTIAL_TARGET)
            self._token = token
        self.accept()

    def apply_to(self, config: Config) -> Config:
        config.repository.owner = self.owner_edit.text().strip()
        config.repository.name = self.name_edit.text().strip()
        folder = self.watch_edit.text().strip()
        if folder:
            config.watch_folders = [folder]
        raw_types = re.split(r"[,;\s]+", self.types_edit.text().strip())
        types = [normalize_extension(value) for value in raw_types if value.strip()]
        types = [value for value in types if value]
        if types:
            config.file_types = types
        archive = self.archive_edit.text().strip()
        if archive:
            config.archive_folder = archive
        config.onboarding_completed = True
        config.normalize()
        return config


def confirm(parent, title: str, text: str, *, detail: str = "",
            ok_text: str = "确定", danger: bool = False) -> bool:
    """统一的二次确认框。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    ok = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is ok


def info(parent, title: str, text: str, *, detail: str = "") -> None:
    """普通提示框。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def warn(parent, title: str, text: str, *, detail: str = "") -> None:
    """出错提示框。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def ask(parent, title: str, text: str) -> bool:
    """是 / 否 询问框，默认「否」。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(text)
    yes = box.addButton("是", QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton("否", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


def describe_bytes(count: int) -> str:
    return human_size(count)
