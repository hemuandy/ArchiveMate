"""可复用的小部件：列表编辑器、拖拽区、状态药丸。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)


class ListEditor(QWidget):
    """带「添加 / 删除」按钮的字符串列表。

    监控文件夹和目标文件类型这两处行为一样，所以共用一个组件。
    添加按钮的文案由调用方决定，真正的取值方式交给 ``on_add``。
    """

    changed = Signal(list)

    def __init__(self, *, add_text: str = "添加", remove_text: str = "删除",
                 empty_hint: str = "", add_title: str = "添加条目",
                 add_prompt: str = "请输入内容：",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._empty_hint = empty_hint
        self._add_title = add_title
        self._add_prompt = add_prompt
        self._add_handler = self._prompt_add

        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setAlternatingRowColors(False)
        self.list.setMinimumHeight(96)

        self.add_button = QPushButton(add_text, self)
        self.remove_button = QPushButton(remove_text, self)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

        self.remove_button.clicked.connect(self.remove_selected)
        self.add_button.clicked.connect(self._request_add)
        self.list.itemSelectionChanged.connect(self._sync_buttons)
        self._sync_buttons()

    # -- 添加按钮的行为 ------------------------------------------------

    def set_add_handler(self, handler) -> None:
        """替换「添加」按钮的行为（默认弹输入框）。传 None 恢复默认。"""
        self._add_handler = handler or self._prompt_add

    def _request_add(self) -> None:
        try:
            self._add_handler()
        except Exception:  # 界面回调不该把整个窗口带崩
            pass

    def _prompt_add(self) -> None:
        text, ok = QInputDialog.getText(self, self._add_title, self._add_prompt)
        if ok:
            self.add_value(text)

    # -- 数据 ---------------------------------------------------------

    def values(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_values(self, values) -> None:
        self.list.clear()
        for value in values:
            text = str(value)
            if not text:
                continue
            if self.find(text) is not None:
                continue
            item = QListWidgetItem(text)
            item.setToolTip(text)
            self.list.addItem(item)
        self._sync_buttons()

    def find(self, text: str) -> int | None:
        needle = text.strip().lower()
        for index in range(self.list.count()):
            if self.list.item(index).text().strip().lower() == needle:
                return index
        return None

    def add_value(self, text: str) -> bool:
        text = (text or "").strip()
        if not text or self.find(text) is not None:
            return False
        item = QListWidgetItem(text)
        item.setToolTip(text)
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        self._sync_buttons()
        self.changed.emit(self.values())
        return True

    def add_values(self, values) -> int:
        added = 0
        for value in values:
            if self.add_value(str(value)):
                added += 1
        return added

    def remove_selected(self) -> None:
        rows = sorted((self.list.row(item) for item in self.list.selectedItems()),
                      reverse=True)
        if not rows:
            return
        for row in rows:
            self.list.takeItem(row)
        self._sync_buttons()
        self.changed.emit(self.values())

    # -- 内部 ---------------------------------------------------------

    def _sync_buttons(self) -> None:
        self.remove_button.setEnabled(bool(self.list.selectedItems()))


class DropArea(QFrame):
    """把文件拖进来上传的区域。"""

    filesDropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)
        self.setProperty("dragActive", False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(104)

        title = QLabel("把文件拖到这里", self)
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("支持自动分类后推送到 GitHub；也可以点下面的按钮挑选文件",
                      self)
        hint.setObjectName("dropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addStretch(1)

    # -- 拖拽 ---------------------------------------------------------

    @staticmethod
    def _local_files(event) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths: list[str] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            paths.append(url.toLocalFile())
        return paths

    def _set_active(self, active: bool) -> None:
        self.setProperty("dragActive", bool(active))
        # 属性选择器要靠样式重算才会生效
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._local_files(event):
            event.acceptProposedAction()
            self._set_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._local_files(event):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802
        self._set_active(False)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._set_active(False)
        paths = self._local_files(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.filesDropped.emit(paths)


class StatusPill(QLabel):
    """顶部那个小小的状态标签。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("pill")
        self.set_state("idle")

    def set_state(self, state: str, text: str | None = None) -> None:
        self.setProperty("state", state)
        if text is not None:
            self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)


def shorten_path(path: Path | str, limit: int = 46) -> str:
    """把过长的路径压成 `D:\\…\\目录\\文件` 的样子。"""
    text = str(path)
    if len(text) <= limit:
        return text
    parts = Path(text).parts
    if len(parts) <= 2:
        return text
    head = parts[0]
    tail = parts[-1]
    return f"{head}…{tail}" if len(head) + len(tail) + 1 < limit else f"…{text[-limit:]}"
