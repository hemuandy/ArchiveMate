"""ArchiveMate 图形界面（PySide6）。"""

from __future__ import annotations

__all__ = ["run", "MainWindow"]


def run(argv: list[str] | None = None) -> int:
    from .main_window import run as _run
    return _run(argv)


def __getattr__(name: str):
    # 延迟导入：只有真正用到 MainWindow 时才加载这个重模块
    if name == "MainWindow":
        from .main_window import MainWindow
        return MainWindow
    raise AttributeError(name)
