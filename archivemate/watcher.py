"""文件夹监控。

用轮询实现（Qt / 标准库都能跑，且对网络盘、OneDrive 同步目录比
事件式监听更可靠）。每个文件必须连续两次扫描大小与修改时间都没变，
才会被判定为「已经下载完」，避免抓到半个文件。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

Logger = Callable[[str], None]

IGNORED_DIR_NAMES = {
    "$recycle.bin", "system volume information", "recycler",
    "windows", "program files", "program files (x86)", "appdata",
    "node_modules", "__pycache__",
}


def _noop(_message: str) -> None:
    pass


def iter_candidate_files(
    folders: Iterable[Path],
    extensions: set[str],
    *,
    should_skip: Callable[[Path], bool] | None = None,
    max_depth: int = 24,
) -> Iterable[Path]:
    """深度优先遍历监控目录，产出扩展名匹配的文件。"""
    skip = should_skip or (lambda _p: False)
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(folder, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        if name.startswith(".") or name.lower() in IGNORED_DIR_NAMES:
                            continue
                        if skip(path):
                            continue
                        stack.append((path, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name.startswith("~$") or entry.name.startswith(".~"):
                            continue  # Office / WPS 的临时文件
                        if skip(path):
                            continue
                        if path.suffix.lower() in extensions:
                            yield path
                except OSError:
                    continue


class FolderWatcher:
    """后台线程：周期性扫描监控目录，把「稳定下来的新文件」回调出去。"""

    def __init__(
        self,
        *,
        folders_provider: Callable[[], list[Path]],
        extensions_provider: Callable[[], list[str]],
        on_file: Callable[[Path, str], None],
        should_skip: Callable[[Path], bool] | None = None,
        is_handled: Callable[[Path], bool] | None = None,
        interval: float = 5.0,
        stability: float = 3.0,
        logger: Logger | None = None,
    ) -> None:
        self._folders = folders_provider
        self._extensions = extensions_provider
        self._on_file = on_file
        self._should_skip = should_skip or (lambda _p: False)
        self._is_handled = is_handled or (lambda _p: False)
        self.interval = max(1.0, float(interval))
        self.stability = max(0.5, float(stability))
        self._log: Logger = logger or _noop

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        #: path -> (size, mtime, 首次看到该指纹的时间)
        self._candidates: dict[str, tuple[int, float, float]] = {}
        self._lock = threading.Lock()
        self._last_scan_at = 0.0
        self._detected_count = 0

    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ArchiveMateWatcher", daemon=True)
        self._thread.start()
        self._log(f"已启动文件夹监控（每 {self.interval:g} 秒扫描一次）")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def apply_settings(self, *, interval: float, stability: float) -> None:
        self.interval = max(1.0, float(interval))
        self.stability = max(0.5, float(stability))

    # ------------------------------------------------------------------

    def current_extensions(self) -> set[str]:
        return {ext.lower() for ext in self._extensions() if ext}

    def scan_existing(self) -> list[Path]:
        """立即列出监控目录里当前所有匹配文件（供「扫描现有文件」按钮用）。"""
        found = list(iter_candidate_files(
            self._folders(), self.current_extensions(), should_skip=self._should_skip))
        return found

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as exc:  # 后台线程绝不能因为单次异常退出
                self._log(f"监控扫描出错：{exc}")
            self._stop_event.wait(self.interval)

    def _poll(self) -> None:
        extensions = self.current_extensions()
        if not extensions:
            self._candidates = {}
            return
        now = time.time()
        previous = self._candidates
        fresh: dict[str, tuple[int, float, float]] = {}

        for path in iter_candidate_files(
                self._folders(), extensions, should_skip=self._should_skip):
            key = str(path)
            try:
                if self._is_handled(path):
                    continue
                stat = path.stat()
            except OSError:
                continue
            size = int(stat.st_size)
            if size == 0:
                continue
            mtime = float(stat.st_mtime)

            known = previous.get(key)
            if known is not None and known[0] == size and known[1] == mtime:
                first_seen = known[2]
            else:
                first_seen = now

            if now - first_seen >= self.stability:
                self._emit(path)
            else:
                fresh[key] = (size, mtime, first_seen)

        self._candidates = fresh
        self._last_scan_at = now

    def _emit(self, path: Path) -> None:
        try:
            self._on_file(path, "watch")
            self._detected_count += 1
            self._log(f"发现新文件：{path.name}")
        except Exception as exc:
            self._log(f"处理新文件失败（{path.name}）：{exc}")

    def mark_handled(self, path: Path) -> None:
        self._candidates.pop(str(path), None)

    @property
    def stats(self) -> tuple[float, int]:
        return (self._last_scan_at, self._detected_count)

    def candidate_count(self) -> int:
        return len(self._candidates)
