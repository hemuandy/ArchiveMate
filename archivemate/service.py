"""应用服务层：把配置、队列、监控、同步引擎串起来。

GUI 与命令行都只跟这一层打交道，所以核心逻辑可以在没有界面时单独测试。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import app_paths, classify, config as config_module, credentials, logging_setup
from .config import Config
from .fsutil import clear_directory, describe_size_limit, directory_size, human_size
from .git_backend import GitBackend, find_git
from .sync import SyncEngine, SyncOutcome, unique_dest_rel
from .tracker import PendingItem, Tracker, STATUS_BLOCKED, STATUS_FAILED, STATUS_PENDING
from .watcher import FolderWatcher

# 事件类型
EVENT_LOG = "log"
EVENT_QUEUE = "queue"          # 待推送列表发生变化
EVENT_SYNC_START = "sync_start"
EVENT_SYNC_DONE = "sync_done"
EVENT_STATUS = "status"        # 一句状态栏文案
EVENT_PENDING_DETECTED = "detected"


@dataclass
class Event:
    kind: str
    payload: Any = None


class ArchiveMateService:
    """所有后台行为的唯一入口。"""

    def __init__(self, cfg: Config | None = None, *,
                 auto_start_watcher: bool = True,
                 backend_factory: "Callable[[], GitBackend] | None" = None) -> None:
        self.log = logging_setup.get_logger()
        self.config: Config = cfg or config_module.load()
        self.tracker = Tracker()
        self.events: "queue.Queue[Event]" = queue.Queue()

        self.engine = SyncEngine(
            self.config, self.tracker,
            logger=self._log,
            progress=self._progress,
            token_provider=credentials.get_token,
            backend_factory=backend_factory,
        )

        self.watcher = FolderWatcher(
            folders_provider=lambda: self.config.existing_watch_folders(),
            extensions_provider=lambda: list(self.config.file_types),
            on_file=self._on_watched_file,
            should_skip=self.should_skip,
            is_handled=self.is_handled,
            interval=self.config.poll_interval_seconds,
            stability=self.config.stability_seconds,
            logger=self._log,
        )

        self._sync_lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._stopped = False
        self._auto_start = auto_start_watcher

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.tracker.prune_seen()
        self.tracker.save()
        if self._auto_start:
            self.watcher.start()

    def stop(self) -> None:
        self._stopped = True
        self.watcher.stop()
        try:
            self.tracker.save()
        except OSError:
            pass

    @property
    def syncing(self) -> bool:
        return self._sync_thread is not None and self._sync_thread.is_alive()

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def _emit(self, kind: str, payload: Any = None) -> None:
        self.events.put(Event(kind, payload))

    def _log(self, message: str) -> None:
        self.log.info(message)
        self._emit(EVENT_LOG, message)

    def _progress(self, stage: str, message: str) -> None:
        self._emit(EVENT_STATUS, message)

    def drain_events(self, limit: int = 200) -> list[Event]:
        out: list[Event] = []
        for _ in range(limit):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # ------------------------------------------------------------------
    # 路径过滤
    # ------------------------------------------------------------------

    def _guarded_roots(self) -> list[Path]:
        roots: list[Path] = [app_paths.cache_dir(), app_paths.logs_dir()]
        archive = self.config.archive_folder.strip()
        if archive:
            roots.append(Path(archive))
        return roots

    def should_skip(self, path: Path) -> bool:
        """归档文件夹、缓存目录、软件自身目录里的东西一律不抓。"""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for root in self._guarded_roots():
            try:
                resolved.relative_to(root.resolve())
                return True
            except (ValueError, OSError):
                continue
        # 配置文件所在目录里的 .json 之类也会被扩展名过滤掉，这里再兜一层
        try:
            if resolved.name in ("config.json", "state.json"):
                return True
        except OSError:
            pass
        return False

    def is_handled(self, path: Path) -> bool:
        if self.should_skip(path):
            return True
        if self.tracker.find_by_source(path) is not None:
            return True
        return self.tracker.is_known(path)

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------

    def _on_watched_file(self, path: Path, origin: str) -> None:
        accepted, _reason = self.ingest(path, origin=origin)
        if accepted:
            self._emit(EVENT_PENDING_DETECTED, path)

    def ingest(self, path: Path, *, origin: str = "drop") -> tuple[bool, str]:
        """把单个文件加入待推送队列。返回 (是否入队, 说明)。"""
        path = Path(path)
        if not path.is_file():
            return False, "不是有效文件"
        if self.should_skip(path):
            return False, "该文件位于归档/缓存目录内，已跳过"

        existing = self.tracker.find_by_source(path)
        if existing is not None:
            stat = path.stat()
            existing.size = int(stat.st_size)
            existing.mtime = float(stat.st_mtime)
            if existing.status in (STATUS_FAILED, STATUS_BLOCKED):
                existing.status = STATUS_PENDING
                existing.error = ""
            self.tracker.remember(path)
            self.tracker.save()
            self._emit(EVENT_QUEUE)
            return True, "文件已更新为最新版本"

        if not self.config.matches_type(path.name):
            suffix = path.suffix or "（无扩展名）"
            return False, f"扩展名 {suffix} 不在目标文件类型列表里"

        size = path.stat().st_size
        if size >= self.config.limits.block_bytes:
            dest = self._reserve_dest(path)
            item = self.tracker.add(path, dest_rel=dest, origin=origin,
                                    status=STATUS_BLOCKED,
                                    error=describe_size_limit(size))
            self.tracker.save()
            self._emit(EVENT_QUEUE)
            self._log(f"已阻止推送（体积过大）：{path.name} — {human_size(size)}")
            return True, describe_size_limit(size)

        dest = self._reserve_dest(path)
        note = describe_size_limit(size)
        self.tracker.add(path, dest_rel=dest, origin=origin, note=note)
        self.tracker.save()
        self._emit(EVENT_QUEUE)
        self.watcher.mark_handled(path)
        if note:
            self._log(f"提示：{path.name} — {note}")
        return True, f"已加入待推送队列（{classify.human_category(dest)}）"

    def _reserve_dest(self, path: Path) -> str:
        base = classify.build_dest_rel(
            path, date_source=self.config.date_source)
        return unique_dest_rel(base, self.tracker.dest_rels())

    def ingest_many(self, paths: Iterable[Path], *,
                    origin: str = "drop") -> tuple[int, list[tuple[Path, str]]]:
        accepted = 0
        rejected: list[tuple[Path, str]] = []
        for path in paths:
            ok, reason = self.ingest(path, origin=origin)
            if ok:
                accepted += 1
            else:
                rejected.append((path, reason))
        return accepted, rejected

    def scan_existing(self, *, origin: str = "scan") -> tuple[int, int]:
        """扫描监控目录里现有的匹配文件并全部入队。返回 (发现数, 入队数)。"""
        found = self.watcher.scan_existing()
        accepted = 0
        for path in found:
            ok, _reason = self.ingest(path, origin=origin)
            if ok:
                accepted += 1
        self._emit(EVENT_QUEUE)
        return (len(found), accepted)

    # ------------------------------------------------------------------
    # 队列操作
    # ------------------------------------------------------------------

    def remove_item(self, item_id: str) -> bool:
        removed = self.tracker.remove(item_id)
        self.tracker.save()
        self._emit(EVENT_QUEUE)
        return removed is not None

    def remove_items(self, item_ids: Sequence[str]) -> int:
        count = self.tracker.remove_many(item_ids)
        self.tracker.save()
        self._emit(EVENT_QUEUE)
        return count

    def clear_queue(self) -> int:
        count = self.tracker.clear_items()
        self.tracker.save()
        self._emit(EVENT_QUEUE)
        return count

    def mark_pending_again(self, item_ids: Sequence[str]) -> int:
        wanted = set(item_ids)
        count = 0
        for item in self.tracker.items:
            if item.item_id in wanted and item.status == STATUS_FAILED:
                item.status = STATUS_PENDING
                item.error = ""
                count += 1
        self.tracker.save()
        self._emit(EVENT_QUEUE)
        return count

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def sync_async(self, *, item_ids: Sequence[str] | None = None) -> bool:
        """在后台线程里跑一次同步。返回是否成功启动。"""
        if self.syncing:
            self._emit(EVENT_STATUS, "已有同步任务在进行中。")
            return False
        self._sync_thread = threading.Thread(
            target=self._sync_worker, args=(item_ids,),
            name="ArchiveMateSync", daemon=True)
        self._sync_thread.start()
        return True

    def sync_blocking(self, *, item_ids: Sequence[str] | None = None) -> SyncOutcome:
        """同步执行（命令行与测试用）。"""
        with self._sync_lock:
            self._emit(EVENT_SYNC_START)
            self._emit(EVENT_STATUS, "正在同步……")
            outcome = self.engine.run(item_ids=item_ids)
            self.tracker.save()
            self._emit(EVENT_QUEUE)
            self._emit(EVENT_SYNC_DONE, outcome)
            return outcome

    def cancel_sync(self) -> bool:
        """请求中断正在进行的同步。

        返回是否真的打断了正在跑的 git 命令（同步还没开始或已经结束时为 False）。
        """
        if not self.syncing:
            return False
        stopped = self.engine.cancel()
        if stopped:
            self._log("已请求中断同步，正在停止 git 命令……")
            self._emit(EVENT_STATUS, "正在取消同步……")
        return stopped

    def _sync_worker(self, item_ids: Sequence[str] | None) -> None:
        try:
            self.sync_blocking(item_ids=item_ids)
        except Exception as exc:  # 后台线程兜底
            self._log(f"同步时发生未预期的错误：{exc}")
            self._emit(EVENT_STATUS, f"同步失败：{exc}")
            self._emit(EVENT_SYNC_DONE, SyncOutcome(ok=False, message=str(exc)))

    # ------------------------------------------------------------------
    # 归档文件夹管理
    # ------------------------------------------------------------------

    def archive_summary(self) -> tuple[int, int]:
        """归档文件夹的（文件数, 占用字节）。只读，不会创建目录。"""
        try:
            path = self.engine.archive_root(create=False)
        except OSError:
            return (0, 0)
        if not path.is_dir():
            return (0, 0)
        total, count = directory_size(path)   # fsutil 返回的是（字节, 个数）
        return (count, total)

    def clear_archive(self) -> tuple[int, int]:
        """清空归档文件夹。调用方必须先做二次确认。"""
        root = self.engine.archive_root()
        problems = self._archive_clear_guard(root)
        if problems:
            raise OSError(problems)
        removed, freed = clear_directory(root)
        self._log(f"已清空归档文件夹：删除 {removed} 个文件，释放 {human_size(freed)}")
        return removed, freed

    @staticmethod
    def _archive_clear_guard(root: Path) -> str:
        """守住「别把用户的磁盘根目录删了」这条线。"""
        try:
            resolved = root.resolve()
        except OSError:
            return f"无法解析路径：{root}"
        if resolved == Path(resolved.anchor):
            return "归档文件夹不能是磁盘根目录。"
        if resolved == Path.home().resolve():
            return "归档文件夹不能是用户主目录。"
        if len(resolved.parts) <= 2 and resolved.anchor and resolved.parts[1].endswith(":\\"):
            return "归档文件夹太靠近磁盘根目录，请改到更具体的子目录。"
        return ""

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def reload_config(self) -> Config:
        self.config = config_module.load()
        self._apply_config_to_runtime()
        return self.config

    def apply_config(self, cfg: Config, *, persist: bool = True) -> None:
        self.config = cfg
        self.config.normalize()
        if persist:
            config_module.save(self.config)
        self.engine.config = self.config
        self._apply_config_to_runtime()
        self._emit(EVENT_QUEUE)
        self._emit(EVENT_STATUS, "设置已保存。")

    def _apply_config_to_runtime(self) -> None:
        self.watcher.apply_settings(
            interval=self.config.poll_interval_seconds,
            stability=self.config.stability_seconds,
        )

    # ------------------------------------------------------------------
    # 环境自检（给设置界面与引导用）
    # ------------------------------------------------------------------

    def environment_report(self) -> dict[str, str]:
        git_exe = find_git(self.config.git.resolved_executable())
        if app_paths.home_override():
            portable = "自定义数据目录（ARCHIVEMATE_HOME / --home）"
        elif app_paths.is_portable():
            portable = "是（配置随软件目录一起拷贝）"
        else:
            portable = "否（软件目录不可写，已退回用户目录）"
        return {
            "git": git_exe or "未找到",
            "credential_backend": credentials.backend_name(),
            "config_path": str(app_paths.config_path()),
            "state_path": str(app_paths.state_path()),
            "data_root": str(app_paths.portable_root()),
            "portable": portable,
            "log_path": str(logging_setup.log_file_path()),
        }

    def watch_folder_status(self) -> list[tuple[str, bool, str]]:
        out: list[tuple[str, bool, str]] = []
        for raw in self.config.watch_folders:
            path = Path(raw)
            if not path.exists():
                out.append((raw, False, "目录不存在"))
            elif not path.is_dir():
                out.append((raw, False, "不是目录"))
            else:
                try:
                    next(path.iterdir(), None)
                    out.append((raw, True, "正常"))
                except OSError as exc:
                    out.append((raw, False, f"无法读取：{exc}"))
        return out
