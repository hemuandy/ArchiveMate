"""主窗口：监控列表、拖拽区、待推送队列、日志与状态。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor, QDesktopServices, QGuiApplication, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import APP_DISPLAY_NAME, __version__, app_paths, classify
from ..config import SYNC_INTERVAL_CHOICES
from ..fsutil import human_size
from ..service import (
    EVENT_LOG, EVENT_QUEUE, EVENT_STATUS, EVENT_SYNC_DONE, EVENT_SYNC_START,
    ArchiveMateService,
)
from ..tracker import ORIGIN_LABELS, STATUS_LABELS, STATUS_PENDING
from .dialogs import OnboardingDialog, SettingsDialog, ask, confirm, info, warn
from .theme import COLORS
from .widgets import DropArea, ListEditor, StatusPill, shorten_path

QUEUE_COLUMNS = ("文件", "目标位置", "大小", "来源", "状态")


class MainWindow(QMainWindow):
    def __init__(self, service: ArchiveMateService | None = None, parent=None) -> None:
        super().__init__(parent)
        self.service = service or ArchiveMateService()
        self._ready = False

        self.setWindowTitle(f"{APP_DISPLAY_NAME} v{__version__}")
        ui = self.service.config.ui
        self.resize(int(ui.window_width), int(ui.window_height))
        self.setMinimumSize(880, 600)
        self.setAcceptDrops(True)   # 整个窗口都能接收拖拽

        self._build_ui()
        self._wire()

        self.service.start()
        self._ready = True

        self._pump = QTimer(self)
        self._pump.setInterval(200)
        self._pump.timeout.connect(self._drain_events)
        self._pump.start()

        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._auto_sync)

        self._load_config_into_ui()
        self._refresh_queue()
        self._refresh_archive_label()
        self._apply_interval()
        self._log(f"{APP_DISPLAY_NAME} v{__version__} 已启动，共 "
                  f"{len(self.service.config.existing_watch_folders())} 个监控文件夹。")

        if not self.service.config.onboarding_completed:
            QTimer.singleShot(300, self._maybe_onboard)

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        header = QHBoxLayout()
        title = QLabel(APP_DISPLAY_NAME, central)
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        self.pill = StatusPill("待机", central)
        header.addWidget(title)
        header.addWidget(self.pill)
        header.addStretch(1)

        self.sync_button = QPushButton("立即同步", central)
        self.sync_button.setObjectName("primary")
        self.cancel_button = QPushButton("取消同步", central)
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setVisible(False)
        self.scan_button = QPushButton("扫描现有文件", central)
        self.settings_button = QPushButton("设置", central)
        for button in (self.sync_button, self.cancel_button,
                       self.scan_button, self.settings_button):
            header.addWidget(button)

        left = QWidget(central)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        self.watch_box = QGroupBox("监控文件夹", left)
        watch_layout = QVBoxLayout(self.watch_box)
        self.watch_editor = ListEditor(add_text="添加文件夹", remove_text="移除",
                                       empty_hint="还没有监控任何文件夹", parent=self.watch_box)
        watch_layout.addWidget(self.watch_editor)

        self.types_box = QGroupBox("目标文件类型", left)
        types_layout = QVBoxLayout(self.types_box)
        self.types_editor = ListEditor(add_text="添加类型", remove_text="移除",
                                       parent=self.types_box)
        type_row = QHBoxLayout()
        self.type_input = QLineEdit(self.types_box)
        self.type_input.setPlaceholderText("例如 .pptx")
        add_type = QPushButton("添加", self.types_box)
        add_type.clicked.connect(self._add_type_from_input)
        self.type_input.returnPressed.connect(self._add_type_from_input)
        type_row.addWidget(self.type_input, 1)
        type_row.addWidget(add_type)
        types_layout.addLayout(type_row)
        types_layout.addWidget(self.types_editor)

        self.sync_box = QGroupBox("同步", left)
        sync_layout = QVBoxLayout(self.sync_box)
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("定时同步", self.sync_box))
        self.interval_combo = QComboBox(self.sync_box)
        for minutes, label in SYNC_INTERVAL_CHOICES:
            self.interval_combo.addItem(label, minutes)
        interval_row.addWidget(self.interval_combo, 1)
        sync_layout.addLayout(interval_row)

        self.archive_box = QGroupBox("归档文件夹", left)
        archive_layout = QVBoxLayout(self.archive_box)
        self.archive_label = QLabel("", self.archive_box)
        self.archive_label.setObjectName("hint")
        self.archive_label.setWordWrap(True)
        archive_buttons = QHBoxLayout()
        open_archive = QPushButton("打开", self.archive_box)
        open_archive.clicked.connect(self._open_archive)
        choose_archive = QPushButton("更换", self.archive_box)
        choose_archive.clicked.connect(self._choose_archive)
        self.clear_archive_button = QPushButton("清空归档", self.archive_box)
        self.clear_archive_button.setObjectName("danger")
        self.clear_archive_button.clicked.connect(self._clear_archive)
        archive_buttons.addWidget(open_archive)
        archive_buttons.addWidget(choose_archive)
        archive_buttons.addWidget(self.clear_archive_button)
        archive_layout.addWidget(self.archive_label)
        archive_layout.addLayout(archive_buttons)

        left_layout.addWidget(self.watch_box, 1)
        left_layout.addWidget(self.types_box, 1)
        left_layout.addWidget(self.sync_box)
        left_layout.addWidget(self.archive_box)
        left.setMaximumWidth(330)

        right = QWidget(central)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        self.drop_area = DropArea(right)
        right_layout.addWidget(self.drop_area)

        pick_row = QHBoxLayout()
        pick_files = QPushButton("挑选文件上传…", right)
        pick_files.clicked.connect(self._pick_files)
        self.retry_button = QPushButton("重新排队", right)
        self.retry_button.clicked.connect(self._retry_selected)
        self.remove_button = QPushButton("移除选中", right)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = QPushButton("清空队列", right)
        self.clear_button.setObjectName("danger")
        self.clear_button.clicked.connect(self._clear_queue)
        pick_row.addWidget(pick_files)
        pick_row.addStretch(1)
        pick_row.addWidget(self.retry_button)
        pick_row.addWidget(self.remove_button)
        pick_row.addWidget(self.clear_button)
        right_layout.addLayout(pick_row)

        queue_box = QGroupBox("待推送队列", right)
        queue_layout = QVBoxLayout(queue_box)
        self.queue_table = QTableWidget(0, len(QUEUE_COLUMNS), queue_box)
        self.queue_table.setHorizontalHeaderLabels(list(QUEUE_COLUMNS))
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setAlternatingRowColors(True)
        header_view = self.queue_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, len(QUEUE_COLUMNS)):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.setColumnWidth(0, 190)
        queue_layout.addWidget(self.queue_table)

        self.log_box = QGroupBox("日志", right)
        log_layout = QVBoxLayout(self.log_box)
        self.log_view = QPlainTextEdit(self.log_box)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(90)
        log_layout.addWidget(self.log_view)

        self.splitter = QSplitter(Qt.Orientation.Vertical, right)
        self.splitter.addWidget(queue_box)
        self.splitter.addWidget(self.log_box)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        right_layout.addWidget(self.splitter, 1)

        body = QSplitter(Qt.Orientation.Horizontal, central)
        body.addWidget(left)
        body.addWidget(right)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(body, 1)

        self.status_label = QLabel("", self)
        self.statusBar().addWidget(self.status_label)

    def _wire(self) -> None:
        self.sync_button.clicked.connect(self._sync_now)
        self.cancel_button.clicked.connect(self._cancel_sync)
        self.scan_button.clicked.connect(self._scan_existing)
        self.settings_button.clicked.connect(self._open_settings)
        self.interval_combo.currentIndexChanged.connect(self._on_interval_changed)
        self.watch_editor.changed.connect(self._on_watch_changed)
        self.types_editor.changed.connect(self._on_types_changed)
        self.watch_editor.set_add_handler(self._add_watch_folder)
        self.drop_area.filesDropped.connect(self._on_files_dropped)
        self.queue_table.itemSelectionChanged.connect(self._sync_buttons)
        QShortcut(QKeySequence.StandardKey.Delete, self.queue_table,
                  activated=self._remove_selected)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._sync_now)

    # ------------------------------------------------------------------
    # 配置 → 界面
    # ------------------------------------------------------------------

    def _load_config_into_ui(self) -> None:
        config = self.service.config
        self.watch_editor.set_values(config.watch_folders)
        self.types_editor.set_values(config.file_types)
        index = self.interval_combo.findData(config.sync_interval_minutes)
        if index >= 0 and index != self.interval_combo.currentIndex():
            blocked = self.interval_combo.blockSignals(True)
            self.interval_combo.setCurrentIndex(index)
            self.interval_combo.blockSignals(blocked)
        self._sync_buttons()

    def _refresh_archive_label(self) -> None:
        config = self.service.config
        root = config.archive_folder.strip() or str(app_paths.default_archive_dir())
        files, size = self.service.archive_summary()
        self.archive_label.setText(
            f"{shorten_path(root, 40)}\n已归档 {files} 个文件，占用 {human_size(size)}")
        self.archive_label.setToolTip(root)

    # ------------------------------------------------------------------
    # 事件泵
    # ------------------------------------------------------------------

    def _drain_events(self) -> None:
        for event in self.service.drain_events():
            if event.kind == EVENT_LOG:
                self._log(str(event.payload))
            elif event.kind == EVENT_QUEUE:
                self._refresh_queue()
            elif event.kind == EVENT_STATUS:
                self.status_label.setText(str(event.payload))
            elif event.kind == EVENT_SYNC_START:
                self._set_syncing(True)
            elif event.kind == EVENT_SYNC_DONE:
                self._set_syncing(False)
                self._on_sync_done(event.payload)

    def _on_sync_done(self, outcome) -> None:
        self._refresh_queue()
        self._refresh_archive_label()
        if outcome is None:
            return
        if getattr(outcome, "ok", False) and getattr(outcome, "pushed_count", 0):
            self.pill.set_state("ok", f"已推送 {outcome.pushed_count} 个")
            self.status_label.setText(
                f"推送完成：{outcome.pushed_count} 个文件，"
                f"{human_size(getattr(outcome, 'pushed_bytes', 0))}")
        elif getattr(outcome, "ok", False):
            self.pill.set_state("idle", "待机")
            self.status_label.setText(getattr(outcome, "message", "") or "没有待推送的文件。")
        else:
            self.pill.set_state("error", "同步失败")
            self.status_label.setText(getattr(outcome, "message", "") or "同步失败。")
        blocked = getattr(outcome, "blocked", None)
        if blocked:
            self._log(f"有 {len(blocked)} 个文件超过大小上限，已跳过。")

    def _set_syncing(self, syncing: bool) -> None:
        self.sync_button.setEnabled(not syncing)
        self.sync_button.setText("正在同步…" if syncing else "立即同步")
        self.scan_button.setEnabled(not syncing)
        # 同步卡住时，用户必须有一个能按的按钮
        self.cancel_button.setVisible(syncing)
        self.cancel_button.setEnabled(syncing)
        if syncing:
            self.pill.set_state("busy", "同步中")
        elif self.pill.property("state") == "busy":
            self.pill.set_state("idle", "待机")

    def _cancel_sync(self) -> None:
        if not self.service.cancel_sync():
            self._log("当前没有正在进行的同步。")
            return
        self.cancel_button.setEnabled(False)
        self.pill.set_state("busy", "正在取消")

    def _log(self, message: str) -> None:
        if not message:
            return
        self.log_view.appendPlainText(message)

    # ------------------------------------------------------------------
    # 队列表格
    # ------------------------------------------------------------------

    def _refresh_queue(self) -> None:
        items = list(self.service.tracker.items)
        self.queue_table.setRowCount(len(items))
        for row, item in enumerate(items):
            name_item = QTableWidgetItem(item.name)
            name_item.setData(Qt.ItemDataRole.UserRole, item.item_id)
            name_item.setToolTip(str(item.source_path))
            self.queue_table.setItem(row, 0, name_item)

            dest = QTableWidgetItem(item.dest_rel)
            dest.setToolTip(item.dest_rel)
            self.queue_table.setItem(row, 1, dest)

            size = QTableWidgetItem(human_size(item.size))
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
            self.queue_table.setItem(row, 2, size)

            origin = QTableWidgetItem(ORIGIN_LABELS.get(item.origin, item.origin))
            self.queue_table.setItem(row, 3, origin)

            label = STATUS_LABELS.get(item.status, item.status)
            if item.note and item.status == STATUS_PENDING:
                label += "（偏大）"
            status = QTableWidgetItem(label)
            detail = item.error or item.note or ""
            status.setToolTip(detail)
            colour = {
                STATUS_PENDING: COLORS["text"],
                "failed": COLORS["error"],
                "blocked": COLORS["warn"],
                "missing": COLORS["muted"],
            }.get(item.status)
            if colour:
                status.setForeground(QColor(colour))
            self.queue_table.setItem(row, 4, status)

        summary = f"待推送 {len(items)} 个文件"
        history = len(self.service.tracker.history)
        if history:
            summary += f"　|　已归档 {history} 个"
        self.status_label.setText(summary)
        self._sync_buttons()

    def _selected_item_ids(self) -> list[str]:
        ids: list[str] = []
        for index in self.queue_table.selectionModel().selectedRows():
            cell = self.queue_table.item(index.row(), 0)
            if cell is not None:
                ids.append(str(cell.data(Qt.ItemDataRole.UserRole)))
        return ids

    def _sync_buttons(self) -> None:
        has_selection = bool(self._selected_item_ids())
        self.remove_button.setEnabled(has_selection)
        self.retry_button.setEnabled(has_selection)
        self.clear_button.setEnabled(self.queue_table.rowCount() > 0)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def _save_config(self) -> None:
        try:
            self.service.apply_config(self.service.config)
        except Exception as exc:
            warn(self, "保存失败", f"配置无法写入磁盘：{exc}")

    def _add_watch_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择要监控的文件夹", "")
        if chosen:
            if self.watch_editor.add_value(chosen):
                self._log(f"已添加监控文件夹：{chosen}")

    def _add_type_from_input(self) -> None:
        text = classify.normalize_extension(self.type_input.text())
        if not text:
            return
        if self.types_editor.add_value(text):
            self.type_input.clear()

    def _on_watch_changed(self, values: list[str]) -> None:
        if not self._ready:
            return
        self.service.config.watch_folders = list(values)
        self.service.apply_config(self.service.config)
        self._log(f"监控文件夹已更新（{len(values)} 个）")

    def _on_types_changed(self, values: list[str]) -> None:
        if not self._ready:
            return
        self.service.config.file_types = list(values)
        self.service.apply_config(self.service.config)
        self._log(f"目标文件类型已更新：{' '.join(values) if values else '（空）'}")

    def _on_interval_changed(self) -> None:
        if not self._ready:
            return
        minutes = int(self.interval_combo.currentData())
        self.service.config.sync_interval_minutes = minutes
        self.service.apply_config(self.service.config)
        self._apply_interval()
        label = dict(SYNC_INTERVAL_CHOICES).get(minutes, "")
        self._log(f"定时同步已设置为：{label}")

    def _apply_interval(self) -> None:
        minutes = int(self.service.config.sync_interval_minutes)
        if minutes > 0:
            self._auto_timer.start(max(1, minutes) * 60 * 1000)
        else:
            self._auto_timer.stop()

    def _auto_sync(self) -> None:
        if self.service.syncing:
            return
        retryable = self.service.tracker.retryable_items()
        if retryable:
            self._log(f"定时同步触发（{len(retryable)} 个文件待推送）。")
            self.service.sync_async()
        else:
            self.service.scan_existing()

    def _sync_now(self) -> None:
        if self.service.syncing:
            return
        problems = self.service.engine.validate()
        if problems:
            if ask(self, "还不能同步", problems[0] + "\n\n现在去设置里填写吗？"):
                self._open_settings()
            return
        if not self.service.tracker.retryable_items():
            self.service.scan_existing()
        self.service.sync_async()

    def _scan_existing(self) -> None:
        added, skipped = self.service.scan_existing()
        self._log(f"扫描完成：新增 {added} 个，跳过 {skipped} 个。")
        self._refresh_queue()

    def _pick_files(self) -> None:
        filters = "支持的文件 (*);;所有文件 (*)"
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要归档的文件", "", filters)
        if paths:
            self._ingest(paths)

    def _on_files_dropped(self, paths: list[str]) -> None:
        self._ingest(paths)

    def _ingest(self, paths: list[str]) -> None:
        accepted, rejected = self.service.ingest_many(Path(p) for p in paths)
        self._refresh_queue()
        if accepted:
            self._log(f"已加入队列：{accepted} 个文件。")
        if rejected:
            self._log(f"有 {rejected} 个文件没有加入队列（类型不匹配或已在队列中）。")
        if accepted or rejected:
            self.raise_()
            self.activateWindow()

    def _remove_selected(self) -> None:
        ids = self._selected_item_ids()
        if not ids:
            return
        count = self.service.remove_items(ids)
        self._log(f"已从队列移除 {count} 个文件（本地文件不会被改动）。")
        self._refresh_queue()

    def _retry_selected(self) -> None:
        ids = self._selected_item_ids()
        if not ids:
            return
        count = self.service.mark_pending_again(ids)
        self._log(f"已重新排队 {count} 个文件。")
        self._refresh_queue()

    def _clear_queue(self) -> None:
        count = self.queue_table.rowCount()
        if not count:
            return
        if not confirm(self, "清空待推送队列",
                       f"确定要移除队列中的 {count} 个文件吗？",
                       detail="只会清空队列记录，本地文件不会被删除，"
                              "之后仍可重新扫描加入。",
                       ok_text="清空"):
            return
        removed = self.service.clear_queue()
        self._log(f"已清空队列，共移除 {removed} 项。")
        self._refresh_queue()

    def _open_archive(self) -> None:
        root = self.service.config.archive_folder.strip() \
            or str(app_paths.default_archive_dir())
        Path(root).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(root))

    def _choose_archive(self) -> None:
        current = self.service.config.archive_folder.strip()
        chosen = QFileDialog.getExistingDirectory(self, "选择归档文件夹", current)
        if not chosen:
            return
        self.service.config.archive_folder = chosen
        self._save_config()
        self._refresh_archive_label()
        self._log(f"归档文件夹已改为：{chosen}")

    def _clear_archive(self) -> None:
        files, size = self.service.archive_summary()
        if not files:
            info(self, "归档文件夹是空的", "目前没有需要清理的文件。")
            return
        root = self.service.config.archive_folder.strip() \
            or str(app_paths.default_archive_dir())
        if not confirm(self, "清空归档文件夹",
                       f"确定要彻底删除归档文件夹里的 {files} 个文件吗？",
                       detail=f"位置：{root}\n将释放约 {human_size(size)}。"
                              "文件已经推送到 GitHub，此操作不可撤销。",
                       ok_text="彻底删除", danger=True):
            return
        try:
            removed, freed = self.service.clear_archive()
        except Exception as exc:
            warn(self, "清理失败", str(exc))
            return
        self._refresh_archive_label()
        self._log(f"已清空归档文件夹：删除 {removed} 个文件，释放 {human_size(freed)}。")
        info(self, "清理完成",
             f"已删除 {removed} 个文件，释放 {human_size(freed)}。")

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.service.config, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        config = dialog.apply_to(self.service.config)
        self.service.apply_config(config)
        self._load_config_into_ui()
        self._refresh_archive_label()
        self._apply_interval()
        self._log("设置已保存。")

    def _maybe_onboard(self) -> None:
        dialog = OnboardingDialog(self.service.config, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            self.service.config.onboarding_completed = True
            self._save_config()
            return
        config = dialog.apply_to(self.service.config)
        self.service.apply_config(config)
        self._load_config_into_ui()
        self._refresh_archive_label()
        self._log("首次设置已完成，可以开始用了。")

    # ------------------------------------------------------------------
    # 窗口级拖拽
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self._ingest(paths)

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            ui = self.service.config.ui
            size = self.size()
            ui.window_width = int(size.width())
            ui.window_height = int(size.height())
            ui.show_logs = self.log_box.isVisible()
            self._save_config()
        except Exception:
            pass
        self._pump.stop()
        self._auto_timer.stop()
        self.service.stop()
        super().closeEvent(event)


def run(argv: list[str] | None = None) -> int:
    """启动图形界面。"""
    app = QApplication.instance() or QApplication(list(argv or sys.argv[:1]))
    app.setApplicationName("ArchiveMate")
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setOrganizationName("ArchiveMate")

    from .theme import STYLESHEET

    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    if os.environ.get("ARCHIVEMATE_GUI_SELFTEST") == "1":
        # 无头自检：构建完界面就退出，用于打包前的冒烟检查
        screen = QGuiApplication.primaryScreen()
        print(f"窗口已构建：{window.windowTitle()} "
              f"队列 {window.queue_table.rowCount()} 行 "
              f"屏幕 {screen.geometry().width() if screen else '?'}x"
              f"{screen.geometry().height() if screen else '?'}")
        # 两个对话框是延迟导入的，PyInstaller 容易漏收，顺手确认它们也能建起来
        from .dialogs import OnboardingDialog, SettingsDialog

        config = window.service.config
        settings = SettingsDialog(config, window)
        onboarding = OnboardingDialog(config, window)
        print(f"对话框已构建：设置「{settings.get_token_button.text()}」"
              f" / 引导「{onboarding.get_token_button.text()}」")
        for dialog in (settings, onboarding):
            dialog.close()
            dialog.deleteLater()
        window.close()
        return 0

    window.show()
    return app.exec()
