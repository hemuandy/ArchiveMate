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
from .schedule import SCHEDULE_CHOICES, SCHEDULE_KEYS

#: 定时同步档位：关闭 / 每天 0 点 / 每周一 0 点 / 每月 1 号 0 点（见 schedule.py）。
SYNC_SCHEDULE_CHOICES: tuple[tuple[str, str], ...] = SCHEDULE_CHOICES

#: 旧版本用「每 N 分钟」，读旧配置时只要开过就折算成「每天 0 点」。
_LEGACY_INTERVAL_KEY = "sync_interval_minutes"

DEFAULT_FILE_TYPES = (".pdf", ".ppt", ".pptx", ".doc", ".docx")

#: 仓库名留空时的兜底值，界面提示里也用它举例。
DEFAULT_REPOSITORY_NAME = "school-files"


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
        return f"{self.owner}/{self.resolved_name()}" if self.owner else ""

    def resolved_name(self) -> str:
        """仓库名留空时的兜底值：新手最容易卡在「不知道该填什么」。"""
        return self.name.strip() or DEFAULT_REPOSITORY_NAME

    def resolved_remote_url(self) -> str:
        if self.remote_url.strip():
            return self.remote_url.strip()
        if self.owner.strip():
            return f"https://github.com/{self.owner.strip()}/{self.resolved_name()}.git"
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
    # 交给 OpenSSL 后端的 CA 包路径。留空 = 需要时自动从系统证书库导出。
    ssl_ca_info: str = ""
    author_name: str = "ArchiveMate"
    author_email: str = "archivemate@localhost"
    retry_count: int = 3
    retry_delay_seconds: float = 3.0
    # 一次网络操作（ls-remote / fetch / push）最多等多久。
    # 这里给得比较宽：首次同步要把整个仓库拉下来，慢网络下几十 MB 花上
    # 十几分钟很正常。真正防「卡死」的是下面的 stall_timeout（git 自己的
    # 低速检测），不是这个总时长上限。
    network_timeout_seconds: int = 1800
    # 连接建立后多久没有数据就放弃（git 自己没有任何默认超时，
    # 网络被黑洞掉时会一直等下去）。
    stall_timeout_seconds: int = 60

    def resolved_executable(self) -> str:
        return self.executable.strip() or "git"

    def resolved_ssl_backend(self) -> str:
        value = (self.ssl_backend or "").strip().lower()
        return value if value in ("schannel", "openssl") else ""

    def resolved_ssl_ca_info(self) -> str:
        return (self.ssl_ca_info or "").strip()

    def resolved_network_timeout(self) -> int:
        try:
            seconds = int(self.network_timeout_seconds)
        except (TypeError, ValueError):
            return 1800
        return min(7200, max(30, seconds))

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
    sync_schedule: str = "off"          # off | daily | weekly | monthly
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
        cfg.sync_schedule = _as_schedule(data)
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
                ssl_ca_info=_as_str(git.get("ssl_ca_info"), ""),
                author_name=_as_str(git.get("author_name"), "ArchiveMate"),
                author_email=_as_str(git.get("author_email"), "archivemate@localhost"),
                retry_count=_as_int(git.get("retry_count"), 3),
                retry_delay_seconds=_as_float(git.get("retry_delay_seconds"), 3.0),
                network_timeout_seconds=_as_int(git.get("network_timeout_seconds"), 1800),
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
        if self.sync_schedule not in SCHEDULE_KEYS:
            self.sync_schedule = "off"
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
        """定时同步档位的界面文案。"""
        from .schedule import schedule_label

        return schedule_label(self.sync_schedule)

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


def _as_schedule(data: dict[str, Any]) -> str:
    """读定时同步档位；没有这个字段就按旧版的「每 N 分钟」折算。"""
    value = data.get("sync_schedule")
    if isinstance(value, str) and value.strip() in SCHEDULE_KEYS:
        return value.strip()
    if _as_int(data.get(_LEGACY_INTERVAL_KEY), 0) > 0:
        return "daily"
    return "off"


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
