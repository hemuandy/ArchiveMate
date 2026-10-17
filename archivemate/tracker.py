"""待推送队列与运行状态的持久化。

state.json 记录了「哪些文件已经处理过」，所以软件重启、同步中断都不会
重复推送，也不会漏掉文件。「同步中断后可重试，不丢文件」靠这里保证。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import app_paths

STATE_VERSION = 1
MAX_HISTORY = 300
MAX_SEEN = 20000

#: 队列项状态
STATUS_PENDING = "pending"     # 等待推送
STATUS_FAILED = "failed"       # 上次推送失败，等待重试
STATUS_BLOCKED = "blocked"     # 体积超限，需要人工处理
STATUS_MISSING = "missing"     # 源文件已不在原位

STATUS_LABELS = {
    STATUS_PENDING: "待推送",
    STATUS_FAILED: "推送失败",
    STATUS_BLOCKED: "已阻止",
    STATUS_MISSING: "源文件丢失",
}

ORIGIN_LABELS = {
    "watch": "自动监控",
    "drop": "拖拽上传",
    "scan": "手动扫描",
}


@dataclass
class PendingItem:
    item_id: str
    source: str
    size: int
    mtime: float
    dest_rel: str
    added_at: float
    origin: str = "watch"
    status: str = STATUS_PENDING
    error: str = ""
    note: str = ""
    attempts: int = 0

    @property
    def source_path(self) -> Path:
        return Path(self.source)

    @property
    def name(self) -> str:
        return self.source_path.name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingItem":
        return cls(
            item_id=str(data.get("item_id") or uuid.uuid4().hex),
            source=str(data.get("source", "")),
            size=int(data.get("size") or 0),
            mtime=float(data.get("mtime") or 0.0),
            dest_rel=str(data.get("dest_rel", "")),
            added_at=float(data.get("added_at") or time.time()),
            origin=str(data.get("origin") or "watch"),
            status=str(data.get("status") or STATUS_PENDING),
            error=str(data.get("error") or ""),
            note=str(data.get("note") or ""),
            attempts=int(data.get("attempts") or 0),
        )


@dataclass
class HistoryEntry:
    name: str
    dest_rel: str
    size: int
    finished_at: float
    archived_to: str = ""
    origin: str = "watch"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEntry":
        return cls(
            name=str(data.get("name", "")),
            dest_rel=str(data.get("dest_rel", "")),
            size=int(data.get("size") or 0),
            finished_at=float(data.get("finished_at") or 0.0),
            archived_to=str(data.get("archived_to") or ""),
            origin=str(data.get("origin") or "watch"),
        )


class Tracker:
    """队列 + 已见文件指纹 + 完成历史。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_paths.state_path()
        self.items: list[PendingItem] = []
        self.history: list[HistoryEntry] = []
        self.seen: dict[str, list[float]] = {}
        self.stats: dict[str, float] = {"pushed_files": 0, "pushed_bytes": 0}
        self.load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.items = [PendingItem.from_dict(d) for d in data.get("pending") or []
                      if isinstance(d, dict)]
        self.history = [HistoryEntry.from_dict(d) for d in data.get("history") or []
                        if isinstance(d, dict)]
        seen = data.get("seen") or {}
        if isinstance(seen, dict):
            self.seen = {
                str(key): [float(v[0]), float(v[1])]
                for key, v in seen.items()
                if isinstance(v, (list, tuple)) and len(v) == 2
            }
        stats = data.get("stats") or {}
        if isinstance(stats, dict):
            self.stats = {
                "pushed_files": float(stats.get("pushed_files") or 0),
                "pushed_bytes": float(stats.get("pushed_bytes") or 0),
            }

    def save(self) -> None:
        if len(self.seen) > MAX_SEEN:
            # 丢掉最旧的记录（字典保持插入顺序）
            for key in list(self.seen)[: len(self.seen) - MAX_SEEN]:
                self.seen.pop(key, None)
        payload = {
            "version": STATE_VERSION,
            "pending": [item.to_dict() for item in self.items],
            "history": [entry.to_dict() for entry in self.history[-MAX_HISTORY:]],
            "seen": self.seen,
            "stats": self.stats,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # 队列操作
    # ------------------------------------------------------------------

    def find(self, item_id: str) -> PendingItem | None:
        for item in self.items:
            if item.item_id == item_id:
                return item
        return None

    def find_by_source(self, source: Path | str) -> PendingItem | None:
        key = _norm(source)
        for item in self.items:
            if _norm(item.source) == key:
                return item
        return None

    def dest_rels(self) -> set[str]:
        return {item.dest_rel for item in self.items}

    def add(self, source: Path, *,
            dest_rel: str,
            origin: str = "watch",
            size: int | None = None,
            mtime: float | None = None,
            status: str = STATUS_PENDING,
            error: str = "",
            note: str = "") -> PendingItem:
        stat = _safe_stat(source)
        resolved_size = size if size is not None else (stat[0] if stat else 0)
        resolved_mtime = mtime if mtime is not None else (stat[1] if stat else 0.0)
        item = PendingItem(
            item_id=uuid.uuid4().hex,
            source=str(source),
            size=resolved_size,
            mtime=resolved_mtime,
            dest_rel=dest_rel,
            added_at=time.time(),
            origin=origin,
            status=status,
            error=error,
            note=note,
        )
        self.items.append(item)
        self.remember(source, origin=origin)
        return item

    def update(self, item_id: str, **changes: Any) -> PendingItem | None:
        item = self.find(item_id)
        if item is None:
            return None
        for key, value in changes.items():
            if hasattr(item, key):
                setattr(item, key, value)
        return item

    def remove(self, item_id: str) -> PendingItem | None:
        for index, item in enumerate(self.items):
            if item.item_id == item_id:
                return self.items.pop(index)
        return None

    def remove_many(self, item_ids: Iterable[str]) -> int:
        wanted = set(item_ids)
        before = len(self.items)
        self.items = [item for item in self.items if item.item_id not in wanted]
        return before - len(self.items)

    def clear_items(self) -> int:
        count = len(self.items)
        self.items.clear()
        return count

    def pending_items(self) -> list[PendingItem]:
        return [item for item in self.items if item.status == STATUS_PENDING]

    def retryable_items(self) -> list[PendingItem]:
        return [item for item in self.items
                if item.status in (STATUS_PENDING, STATUS_FAILED)]

    # ------------------------------------------------------------------
    # 已见指纹
    # ------------------------------------------------------------------

    def is_known(self, source: Path | str) -> bool:
        """路径与当前指纹都一致，才算「处理过」。

        只看路径会把「推送归档后又被重新下载到同名位置」的文件误判成已处理，
        所以这里要求 (size, mtime) 也一致；文件不在原位同样按未知处理。
        """
        entry = self.seen.get(_norm(source))
        if entry is None:
            return False
        stat = _safe_stat(source)
        if stat is None:
            return False
        return int(entry[0]) == stat[0] and float(entry[1]) == stat[1]

    def signature_of(self, source: Path | str) -> tuple[int, float] | None:
        entry = self.seen.get(_norm(source))
        return (int(entry[0]), float(entry[1])) if entry else None

    def remember(self, source: Path | str, *, origin: str = "watch") -> None:
        stat = _safe_stat(source)
        if stat is None:
            return
        self.seen[_norm(source)] = [stat[0], stat[1]]

    def forget(self, source: Path | str) -> None:
        self.seen.pop(_norm(source), None)

    def prune_seen(self) -> int:
        """丢掉已不存在的源文件记录，避免 seen 无限膨胀。"""
        removed = 0
        for key in list(self.seen):
            if not Path(key).exists():
                self.seen.pop(key, None)
                removed += 1
        return removed

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------

    def record_success(self, item: PendingItem, archived_to: str) -> None:
        self.history.append(HistoryEntry(
            name=Path(item.dest_rel).name,
            dest_rel=item.dest_rel,
            size=item.size,
            finished_at=time.time(),
            archived_to=str(archived_to),
            origin=item.origin,
        ))
        self.history = self.history[-MAX_HISTORY:]
        self.stats["pushed_files"] = self.stats.get("pushed_files", 0) + 1
        self.stats["pushed_bytes"] = self.stats.get("pushed_bytes", 0) + item.size

    def clear_history(self) -> None:
        self.history.clear()
        self.stats = {"pushed_files": 0, "pushed_bytes": 0}


def _norm(path: Path | str) -> str:
    text = str(path)
    if os.name == "nt":
        return os.path.normcase(os.path.abspath(text))
    return text


def _safe_stat(path: Path | str) -> tuple[int, float] | None:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (int(st.st_size), float(st.st_mtime))
