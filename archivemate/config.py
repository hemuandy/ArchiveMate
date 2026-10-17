"""配置模型与便携式读写。

Token 不属于配置：它只进系统密钥链（见 credentials.py）。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import CONFIG_SCHEMA_VERSION, app_paths
from .errors import ConfigError
from .fsutil import BATCH_BYTES, BLOCK_BYTES, WARN_BYTES

#: 定时同步可选间隔（分钟），0 表示关闭。
SYNC_INTERVAL_CHOICES: tuple[tuple[int, str], ...] = (
    (0, "关闭"),
    (5, "每 5 分钟"),
    (15, "每 15 分钟"),
    (30, "每 30 分钟"),
    (60, "每 1 小时"),
    (1440, "每天"),
)

DEFAULT_FILE_TYPES = (".pdf", ".ppt", ".pptx", ".doc", ".docx")


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def normalize_extension(value: str) -> str:
    """把 'PDF' / '*.pdf' / '.pdf ' 统一成 '.pdf'。"""
    text = (value or "").strip().lower()
    text = text.lstrip("*").strip()
    if not text:
        return ""
    if not text.startswith("."):
        text = "." + text
    # 只保留第一个扩展名，避免 '.tar.gz' 之类的歧义
    if text.count(".") > 1:
        text = "." + text.split(".")[-1]
    return text


@dataclass
class RepositoryConfig:
    owner: str = ""
    name: str = ""
    branch: str = "main"
    remote_url: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}" if self.owner and self.name else ""

    def resolved_remote_url(self) -> str:
        if self.remote_url.strip():
            return self.remote_url.strip()
        if self.owner and self.name:
            return f"https://github.com/{self.owner}/{self.name}.git"
        return ""

    def is_configured(self) -> bool:
        return bool(self.resolved_remote_url() and self.branch.strip())


@dataclass
class GitConfig:
    executable: str = ""
    proxy: str = ""
    # HTTPS 证书后端。留空 = 跟随 git 自己的判断（Windows 上默认 schannel）。
    # 国内一部分环境里 schannel 会因为安全软件/企业证书出问题，
    # 换成 "openssl" 是标准的绕行办法。
    ssl_backend: str = ""               # "" | "schannel" | "openssl"
    author_name: str = "ArchiveMate"
    author_email: str = "archivemate@localhost"
    retry_count: int = 3
    retry_delay_seconds: float = 3.0
    # 一次网络操作（ls-remote / fetch / push）最多等多久。
    network_timeout_seconds: int = 300
    # 连接建立后多久没有数据就放弃（git 自己没有任何默认超时，
    # 网络被黑洞掉时会一直等下去）。
    stall_timeout_seconds: int = 60

    def resolved_executable(self) -> str:
        return self.executable.strip() or "git"

    def resolved_ssl_backend(self) -> str:
        value = (self.ssl_backend or "").strip().lower()
        return value if value in ("schannel", "openssl") else ""

    def resolved_network_timeout(self) -> int:
        try:
            seconds = int(self.network_timeout_seconds)
        except (TypeError, ValueError):
            return 300
        return min(3600, max(30, seconds))

    def resolved_stall_timeout(self) -> int:
        try:
            seconds = int(self.stall_timeout_seconds)
        except (TypeError, ValueError):
            return 60
        # 0 表示关闭这条保护（不建议），否则至少 15 秒
        if seconds <= 0:
            return 0
        return min(600, max(15, seconds))


@dataclass
class LimitsConfig:
    warn_bytes: int = WARN_BYTES
    block_bytes: int = BLOCK_BYTES
    batch_bytes: int = BATCH_BYTES


@dataclass
class UIConfig:
    window_width: int = 1080
    window_height: int = 720
    show_logs: bool = True


@dataclass
class Config:
    watch_folders: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=lambda: list(DEFAULT_FILE_TYPES))
    archive_folder: str = ""
    sync_interval_minutes: int = 0
    date_source: str = "mtime"          # mtime | now
    stability_seconds: float = 3.0
    poll_interval_seconds: float = 5.0
    onboarding_completed: bool = False
    repository: RepositoryConfig = field(default_factory=RepositoryConfig)
    git: GitConfig = field(default_factory=GitConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    # ---------- 默认值 ----------

    @classmethod
    def default(cls) -> "Config":
        return cls(
            watch_folders=[str(p) for p in app_paths.default_watch_folders()],
            archive_folder=str(app_paths.default_archive_dir()),
        )

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["version"] = CONFIG_SCHEMA_VERSION
        data["_comment"] = (
            "ArchiveMate 便携配置。GitHub Token 不在此文件中，"
            "存放于系统密钥链（Windows 凭据管理器 / macOS 钥匙串 / libsecret）。"
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        if not isinstance(data, dict):
            raise ConfigError("配置根节点必须是 JSON 对象")
        cfg = cls.default()
        cfg.watch_folders = _as_str_list(data.get("watch_folders"), cfg.watch_folders)
        cfg.file_types = _as_str_list(data.get("file_types"), cfg.file_types)
        cfg.archive_folder = _as_str(data.get("archive_folder"), cfg.archive_folder)
        cfg.sync_interval_minutes = _as_int(
            data.get("sync_interval_minutes"), cfg.sync_interval_minutes)
        cfg.date_source = _as_str(data.get("date_source"), cfg.date_source)
        cfg.stability_seconds = _as_float(
            data.get("stability_seconds"), cfg.stability_seconds)
        cfg.poll_interval_seconds = _as_float(
            data.get("poll_interval_seconds"), cfg.poll_interval_seconds)
        cfg.onboarding_completed = bool(
            data.get("onboarding_completed", cfg.onboarding_completed))

        repo = data.get("repository") or {}
        if isinstance(repo, dict):
            cfg.repository = RepositoryConfig(
                owner=_as_str(repo.get("owner"), ""),
                name=_as_str(repo.get("name"), ""),
                branch=_as_str(repo.get("branch"), "main") or "main",
                remote_url=_as_str(repo.get("remote_url"), ""),
            )

        git = data.get("git") or {}
        if isinstance(git, dict):
            cfg.git = GitConfig(
                executable=_as_str(git.get("executable"), ""),
                proxy=_as_str(git.get("proxy"), ""),
                ssl_backend=_as_str(git.get("ssl_backend"), ""),
                author_name=_as_str(git.get("author_name"), "ArchiveMate"),
                author_email=_as_str(git.get("author_email"), "archivemate@localhost"),
                retry_count=_as_int(git.get("retry_count"), 3),
                retry_delay_seconds=_as_float(git.get("retry_delay_seconds"), 3.0),
                network_timeout_seconds=_as_int(git.get("network_timeout_seconds"), 300),
                stall_timeout_seconds=_as_int(git.get("stall_timeout_seconds"), 60),
            )

        limits = data.get("limits") or {}
        if isinstance(limits, dict):
            cfg.limits = LimitsConfig(
                warn_bytes=_as_int(limits.get("warn_bytes"), WARN_BYTES),
                block_bytes=_as_int(limits.get("block_bytes"), BLOCK_BYTES),
                batch_bytes=_as_int(limits.get("batch_bytes"), BATCH_BYTES),
            )

        ui = data.get("ui") or {}
        if isinstance(ui, dict):
            cfg.ui = UIConfig(
                window_width=_as_int(ui.get("window_width"), 1080),
                window_height=_as_int(ui.get("window_height"), 720),
                show_logs=bool(ui.get("show_logs", True)),
            )

        cfg.normalize()
        return cfg

    def normalize(self) -> None:
        """去重、清洗、补默认值。就地修改。"""
        self.watch_folders = _dedupe(_expand(p) for p in self.watch_folders if p.strip())
        self.file_types = _dedupe(
            normalize_extension(t) for t in self.file_types if normalize_extension(t)
        )
        if not self.file_types:
            self.file_types = list(DEFAULT_FILE_TYPES)
        self.archive_folder = _expand(self.archive_folder) if self.archive_folder else ""
        if self.date_source not in ("mtime", "now"):
            self.date_source = "mtime"
        valid_intervals = {m for m, _ in SYNC_INTERVAL_CHOICES}
        if self.sync_interval_minutes not in valid_intervals:
            self.sync_interval_minutes = 0
        self.stability_seconds = max(0.5, float(self.stability_seconds))
        self.poll_interval_seconds = max(1.0, float(self.poll_interval_seconds))
        self.git.retry_count = max(1, int(self.git.retry_count))
        self.git.retry_delay_seconds = max(0.0, float(self.git.retry_delay_seconds))
        self.limits.warn_bytes = max(1, int(self.limits.warn_bytes))
        self.limits.block_bytes = max(self.limits.warn_bytes, int(self.limits.block_bytes))
        self.limits.batch_bytes = max(self.limits.block_bytes, int(self.limits.batch_bytes))

    def copy(self) -> "Config":
        return Config.from_dict(copy.deepcopy(self.to_dict()))

    # ---------- 便捷查询 ----------

    def interval_label(self) -> str:
        for minutes, label in SYNC_INTERVAL_CHOICES:
            if minutes == self.sync_interval_minutes:
                return label
        return "关闭"

    def matches_type(self, filename: str) -> bool:
        suffix = Path(filename).suffix.lower()
        return bool(suffix) and suffix in self.file_types

    def existing_watch_folders(self) -> list[Path]:
        return [Path(p) for p in self.watch_folders if Path(p).is_dir()]


# ---------- 读写 ----------


def load(path: Path | None = None) -> Config:
    target = path or app_paths.config_path()
    if not target.exists():
        cfg = Config.default()
        cfg.normalize()
        return cfg
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {target}: {exc}") from exc
    if not raw.strip():
        cfg = Config.default()
        cfg.normalize()
        return cfg
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON（{target}）：{exc}") from exc
    return Config.from_dict(data)


def save(cfg: Config, path: Path | None = None) -> Path:
    """原子写入，避免中途崩溃留下半截 JSON。"""
    target = path or app_paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


# ---------- 强转助手 ----------


def _as_str(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) else fallback


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_str_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower() if os.name == "nt" else item
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
