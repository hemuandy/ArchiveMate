"""同步引擎：把待推送队列变成一次（或几次）真实的 git 推送，然后归档。

流程：校验 → 分块 → fetch → 排重命名 → 写 blob / 建树 / 提交 → push
→ 成功后把本地源文件移动到归档文件夹 → 记账。
只有推送成功才会移动本地文件；失败则原样留在队列里，下次可重试。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import app_paths, classify, credentials
from .api_backend import MAX_INLINE_BYTES, ApiGitBackend
from .config import Config
from .errors import ArchiveMateError, FileTooLarge, GitError
from .fsutil import check_size, describe_size_limit, human_size, safe_move
from .git_backend import FileToCommit, GitBackend, find_git
from .tracker import PendingItem, Tracker, STATUS_BLOCKED, STATUS_FAILED, STATUS_MISSING

Logger = Callable[[str], None]
Progress = Callable[[str, str], None]


def _noop(_message: str) -> None:
    pass


@dataclass
class SyncOutcome:
    ok: bool = True
    message: str = ""
    commit: str = ""
    pushed: list[PendingItem] = field(default_factory=list)
    archived: list[tuple[PendingItem, Path]] = field(default_factory=list)
    blocked: list[tuple[PendingItem, str]] = field(default_factory=list)
    failed: list[tuple[PendingItem, str]] = field(default_factory=list)
    skipped: list[tuple[PendingItem, str]] = field(default_factory=list)

    @property
    def pushed_count(self) -> int:
        return len(self.pushed)

    @property
    def pushed_bytes(self) -> int:
        return sum(item.size for item in self.pushed)


def unique_dest_rel(dest_rel: str, taken: set[str]) -> str:
    """在 taken 集合内找一个不冲突的仓库内路径。"""
    if dest_rel not in taken:
        return dest_rel
    path = Path(dest_rel)
    parent = path.parent.as_posix()
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = f"{parent}/{stem} ({index}){suffix}" if parent != "." \
            else f"{stem} ({index}){suffix}"
        if candidate not in taken:
            return candidate
    raise ArchiveMateError(f"无法为 {dest_rel} 找到不冲突的文件名")


def build_commit_message(items: Sequence[PendingItem]) -> str:
    """自动生成 commit message，例如 'add: 2026-10-11 数学课件.pptx'。"""
    if not items:
        return "chore: 空提交"
    if len(items) == 1:
        item = items[0]
        stamp = datetime.fromtimestamp(item.mtime or time.time()).strftime("%Y-%m-%d")
        return f"add: {stamp} {Path(item.dest_rel).name}"
    stamp = datetime.now().strftime("%Y-%m-%d")
    lines = [f"add: {stamp} {len(items)} 个文件", ""]
    lines.extend(f"- {item.dest_rel}" for item in items[:30])
    if len(items) > 30:
        lines.append(f"- ...（其余 {len(items) - 30} 个）")
    return "\n".join(lines)


def chunk_by_size(items: Sequence[PendingItem], limit: int) -> list[list[PendingItem]]:
    """把队列按单次推送体积上限切块。"""
    chunks: list[list[PendingItem]] = []
    current: list[PendingItem] = []
    current_size = 0
    for item in items:
        if current and current_size + item.size > limit:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += item.size
    if current:
        chunks.append(current)
    return chunks


class SyncEngine:
    def __init__(
        self,
        config: Config,
        tracker: Tracker,
        *,
        logger: Logger | None = None,
        progress: Progress | None = None,
        token_provider: Callable[[], str | None] | None = None,
        backend_factory: Callable[[], GitBackend] | None = None,
    ) -> None:
        self.config = config
        self.tracker = tracker
        self._log: Logger = logger or _noop
        self._progress: Progress = progress or (lambda _stage, _msg: None)
        self._token_provider = token_provider or credentials.get_token
        # 测试可以注入一个跳过网络传输的后端
        self._backend_factory = backend_factory
        self._backend: GitBackend | None = None
        self._backend_lock = threading.Lock()
        self._cancelled = False
        # 本进程内记住「证书自愈」的结论，下次同步直接用它，不必再撞一次车。
        # 只在内存里，不写配置文件——写盘会有并发覆盖设置的隐患。
        self._tls_repair: tuple[str, str] | None = None

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self) -> bool:
        """请求中断正在进行的同步；返回是否真的打断了正在跑的 git 命令。"""
        self._cancelled = True
        with self._backend_lock:
            backend = self._backend
        if backend is None:
            return False
        return backend.cancel()

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """返回阻塞同步的问题列表（空列表表示可以跑）。"""
        problems: list[str] = []
        if not self.config.repository.is_configured():
            problems.append("还没有配置 GitHub 仓库。请在「设置」里填写仓库，或运行首次引导。")
        if not find_git(self.config.git.resolved_executable()):
            problems.append("找不到 git。请安装 Git 后重启软件，或在设置里指定 git 路径。")
        # 本地/文件协议的远端（测试与自建服务器）不需要 Token
        remote = self.config.repository.resolved_remote_url()
        if remote.startswith(("http://", "https://")) and not self.token:
            problems.append("还没有填写 GitHub Token。请在「设置」里粘贴一个有 repo 权限的 Token。")
        if not self.config.archive_folder.strip():
            problems.append("还没有设置归档文件夹。请在「设置」里选择。")
        return problems

    @property
    def token(self) -> str | None:
        try:
            return self._token_provider()
        except Exception:
            return None

    def archive_root(self, *, create: bool = True) -> Path:
        raw = self.config.archive_folder.strip()
        root = Path(raw) if raw else app_paths.default_archive_dir()
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def build_backend(self, *, force_git: bool = False) -> GitBackend:
        if self._backend_factory is not None:
            backend = self._backend_factory()
        elif (not force_git and self._use_api_backend()):
            backend = ApiGitBackend(
                self.token or "",
                self.config.repository.owner.strip(),
                self.config.repository.resolved_name(),
                self.config.repository.branch,
                proxy=self.config.git.proxy,
                network_timeout=self.config.git.resolved_network_timeout(),
                logger=self._log,
                on_progress=self._progress,
            )
        else:
            ssl_backend = self.config.git.resolved_ssl_backend()
            ssl_ca_info = self.config.git.resolved_ssl_ca_info()
            if self._tls_repair is not None:
                ssl_backend, ssl_ca_info = self._tls_repair
            backend = GitBackend(
                app_paths.repo_cache_dir(),
                self.config.repository.resolved_remote_url(),
                self.config.repository.branch,
                git_exe=find_git(self.config.git.resolved_executable()) or "git",
                proxy=self.config.git.proxy,
                ssl_backend=ssl_backend,
                ssl_ca_info=ssl_ca_info,
                author_name=self.config.git.author_name,
                author_email=self.config.git.author_email,
                token=self.token,
                stall_seconds=self.config.git.resolved_stall_timeout(),
                network_timeout=self.config.git.resolved_network_timeout(),
                logger=self._log,
            )
            backend.on_tls_healed = self._remember_tls_repair
        with self._backend_lock:
            self._backend = backend
        return backend

    def _use_api_backend(self) -> bool:
        """能走在线接口就走在线接口：只上传新文件，不下载仓库历史。"""
        repository = self.config.repository
        return bool(
            self.token
            and repository.owner.strip()
            and repository.resolved_name()
            and repository.branch.strip()
        )

    def _remember_tls_repair(self, backend: GitBackend) -> None:
        """记下自愈结果，让本进程后续同步少走一次弯路。"""
        self._tls_repair = (backend.ssl_backend, backend.ssl_ca_info)

    # ------------------------------------------------------------------
    # 队列准备
    # ------------------------------------------------------------------

    def _refresh(self, items: Iterable[PendingItem]) -> tuple[list[PendingItem], list[tuple[PendingItem, str]]]:
        """过滤出真正可以推送的项，其余的标记状态并返回。"""
        ready: list[PendingItem] = []
        rejected: list[tuple[PendingItem, str]] = []
        for item in items:
            source = item.source_path
            if not source.is_file():
                item.status = STATUS_MISSING
                item.error = "源文件已不在原位"
                rejected.append((item, item.error))
                continue
            try:
                stat = source.stat()
            except OSError as exc:
                item.status = STATUS_MISSING
                item.error = f"无法读取源文件：{exc}"
                rejected.append((item, item.error))
                continue

            item.size = int(stat.st_size)
            item.mtime = float(stat.st_mtime)

            if item.size >= self.config.limits.block_bytes:
                item.status = STATUS_BLOCKED
                item.error = describe_size_limit(item.size)
                rejected.append((item, item.error))
                continue
            if item.size >= self.config.limits.warn_bytes:
                item.note = describe_size_limit(item.size)

            if item.status in (STATUS_MISSING, STATUS_BLOCKED):
                item.status = STATUS_FAILED
                item.error = ""
            ready.append(item)
        return ready, rejected

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(self, *, item_ids: Sequence[str] | None = None,
            automatic: bool = False) -> SyncOutcome:
        outcome = SyncOutcome()
        self._cancelled = False
        with self._backend_lock:
            self._backend = None

        problems = self.validate()
        if problems:
            outcome.ok = False
            outcome.message = problems[0]
            for problem in problems:
                self._log(f"无法同步：{problem}")
            return outcome

        if item_ids is None:
            candidates = self.tracker.retryable_items()
        else:
            wanted = set(item_ids)
            candidates = [item for item in self.tracker.items if item.item_id in wanted]

        # 队列里已经被标记为「超过上限」的项，用户也应该看到，而不是被静默忽略
        reserved = {item.item_id for item in candidates}
        pre_blocked = [item for item in self.tracker.items
                       if item.status == STATUS_BLOCKED and item.item_id not in reserved]

        if not candidates:
            outcome.ok = False
            if pre_blocked:
                outcome.blocked.extend(
                    (item, item.error or describe_size_limit(item.size))
                    for item in pre_blocked)
                outcome.message = (f"{len(pre_blocked)} 个文件超过单文件上限，"
                                   f"已跳过（可压缩后重试）。")
            else:
                outcome.message = "没有待推送的文件。"
            return outcome

        ready, rejected = self._refresh(candidates)
        for item, reason in rejected:
            if item.status == STATUS_BLOCKED:
                outcome.blocked.append((item, reason))
            else:
                outcome.skipped.append((item, reason))
        outcome.blocked.extend(
            (item, item.error or describe_size_limit(item.size))
            for item in pre_blocked)
        self.tracker.save()

        if not ready:
            outcome.ok = False
            outcome.message = (f"{len(rejected)} 个文件无法推送，详情见日志。"
                               if rejected else "没有可推送的文件。")
            return outcome

        try:
            archive_root = self.archive_root()
        except OSError as exc:
            outcome.ok = False
            outcome.message = f"无法创建归档文件夹：{exc}"
            return outcome

        try:
            # 超大的单个文件放不进在线接口的 JSON，只能退回 git 命令行（要下载整个仓库）
            biggest = max((item.size for item in ready), default=0)
            force_git = biggest > MAX_INLINE_BYTES
            if force_git:
                self._log(
                    f"有 {biggest / 1024 / 1024:.0f} MB 的文件，超出在线接口上限，"
                    "改用 git 命令行推送（需要先把整个仓库下载到本地）。")
            backend = self.build_backend(force_git=force_git)
            self._progress("fetch", "正在读取远端仓库状态……")
            backend.fetch()
        except GitError as exc:
            outcome.ok = False
            outcome.message = str(exc)
            if self._cancelled:
                outcome.message = "同步已取消。"
                outcome.failed.clear()
                self._log("同步已取消，队列内容原样保留。")
                return outcome
            for item in ready:
                item.status = STATUS_FAILED
                item.error = str(exc)
                item.attempts += 1
            self.tracker.save()
            outcome.failed.extend((item, str(exc)) for item in ready)
            self._log(f"同步失败：{exc}")
            return outcome

        if self._cancelled:
            outcome.ok = False
            outcome.message = "同步已取消。"
            self._log("同步已取消，队列内容原样保留。")
            return outcome

        # 与远端已有文件排重：仓库里已经有同路径文件时改名，不要覆盖历史版本
        existing = backend.existing_paths()
        reserved = {other.dest_rel for other in (*candidates, *pre_blocked)}
        for item in ready:
            # 自己占用的路径不算冲突，但仓库里已存在的同名文件必须改名
            taken = existing | (reserved - {item.dest_rel})
            resolved = unique_dest_rel(item.dest_rel, taken)
            if resolved != item.dest_rel:
                self._log(f"仓库中已有同名文件，重命名为 {Path(resolved).name}")
                item.dest_rel = resolved
            existing.add(item.dest_rel)

        batches = chunk_by_size(ready, self.config.limits.batch_bytes)
        if len(batches) > 1:
            self._log(f"本次内容超过单次推送上限，将分 {len(batches)} 次提交。")

        total_pushed = 0
        last_commit = ""
        for index, batch in enumerate(batches, start=1):
            if self._cancelled:
                outcome.ok = False
                outcome.message = "同步已取消。"
                self._log("同步已取消，队列内容原样保留。")
                break
            label = f"[{index}/{len(batches)}] " if len(batches) > 1 else ""
            message = build_commit_message(batch)
            files = [FileToCommit(item.source_path, item.dest_rel) for item in batch]
            total = sum(item.size for item in batch)
            self._progress("push", f"{label}正在推送 {len(batch)} 个文件（{human_size(total)}）……")
            self._log(f"{label}提交信息：{message.splitlines()[0]}")

            try:
                commit = backend.push_with_retry(
                    files, message,
                    attempts=self.config.git.retry_count,
                    delay=self.config.git.retry_delay_seconds,
                )
            except ArchiveMateError as exc:
                if self._cancelled:
                    outcome.ok = False
                    outcome.message = "同步已取消。"
                    self._log("同步已取消，队列内容原样保留。")
                    break
                for item in batch:
                    item.status = STATUS_FAILED
                    item.error = str(exc)
                    item.attempts += 1
                outcome.failed.extend((item, str(exc)) for item in batch)
                outcome.ok = False
                outcome.message = str(exc)
                self.tracker.save()
                continue

            last_commit = commit or last_commit
            self._progress("archive", f"{label}推送成功，正在归档本地文件……")
            for item in batch:
                self._archive_item(item, archive_root, outcome)
            total_pushed += len(batch)
            self.tracker.save()

        outcome.commit = last_commit
        outcome.pushed = [item for item in ready if item.status not in (STATUS_FAILED,)]
        outcome.ok = outcome.ok and not outcome.failed

        if total_pushed:
            outcome.message = (f"已推送 {total_pushed} 个文件"
                               f"（{human_size(outcome.pushed_bytes)}）。")
            self._log(outcome.message)
        elif outcome.blocked:
            outcome.message = f"{len(outcome.blocked)} 个文件因体积过大被阻止。"
        elif not outcome.message:
            outcome.message = "没有文件被推送。"

        return outcome

    # ------------------------------------------------------------------

    def _archive_item(self, item: PendingItem, archive_root: Path,
                      outcome: SyncOutcome) -> None:
        """推送成功后把源文件移到归档文件夹，保持与仓库一致的目录结构。"""
        source = item.source_path
        relative = Path(item.dest_rel)
        try:
            target_dir = archive_root / relative.parent
            target = safe_move(source, target_dir, relative.name)
        except OSError as exc:
            item.status = STATUS_FAILED
            item.error = f"已推送，但移动到归档文件夹失败：{exc}"
            item.attempts += 1
            outcome.failed.append((item, item.error))
            outcome.ok = False
            self._log(f"归档失败（{source.name}）：{exc}")
            return

        self.tracker.record_success(item, str(target))
        self.tracker.remove(item.item_id)
        self.tracker.remember(source)
        outcome.archived.append((item, target))
        self._log(f"已归档：{target}")


def check_file_for_ingest(path: Path, config: Config) -> str:
    """新增文件时的快速体检，返回错误信息（空串表示通过）。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"无法读取文件：{exc}"
    try:
        check_size(size, hard=True)
    except FileTooLarge as exc:
        return str(exc)
    return ""
