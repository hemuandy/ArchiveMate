"""便携式路径解析。

配置文件、状态文件、日志与本地 Git 缓存都放在「软件所在目录」下，
这样整个文件夹拷走就能跟着走；如果该目录不可写（例如装在
Program Files 里），再退回用户数据目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

_cached_root: Path | None = None
_cached_app_dir: Path | None = None


def app_dir() -> Path:
    """软件所在目录：打包后是 exe 所在目录，开发时是项目根目录。"""
    global _cached_app_dir
    if _cached_app_dir is None:
        if getattr(sys, "frozen", False):
            _cached_app_dir = Path(sys.executable).resolve().parent
        else:
            _cached_app_dir = Path(__file__).resolve().parent.parent
    return _cached_app_dir


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".am-write-probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def user_data_dir() -> Path:
    """软件目录不可写时的退路。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def portable_root() -> Path:
    """承载 config.json / state.json / logs / cache 的根目录。

    环境变量 ARCHIVEMATE_HOME 可以覆盖它（自动化测试与多份配置并存时使用）。
    """
    global _cached_root
    if _cached_root is None:
        override = os.environ.get("ARCHIVEMATE_HOME")
        if override:
            _cached_root = Path(override).expanduser()
            _cached_root.mkdir(parents=True, exist_ok=True)
        else:
            candidate = app_dir()
            _cached_root = candidate if _is_writable(candidate) else user_data_dir()
            _cached_root.mkdir(parents=True, exist_ok=True)
    return _cached_root


def reset_cache() -> None:
    """清掉路径缓存（切换 ARCHIVEMATE_HOME 或测试隔离时用）。"""
    global _cached_root, _cached_app_dir
    _cached_root = None
    _cached_app_dir = None


def home_override() -> str:
    """是否用 ARCHIVEMATE_HOME 指定了数据目录。"""
    return os.environ.get("ARCHIVEMATE_HOME", "")


def is_portable() -> bool:
    """配置文件是否真的跟着软件走（而不是落在用户目录或指定目录）。"""
    return not home_override() and portable_root() == app_dir()


def config_path() -> Path:
    return portable_root() / "config.json"


def state_path() -> Path:
    return portable_root() / "state.json"


def logs_dir() -> Path:
    d = portable_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = portable_root() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_cache_dir() -> Path:
    """本地 Git 缓存仓库（bare，不保留工作区副本，避免重复占用磁盘）。"""
    return cache_dir() / "repo.git"


def lock_path() -> Path:
    return cache_dir() / "app.lock"


def default_archive_dir() -> Path:
    return Path.home() / "ArchiveMate归档"


def default_watch_folders() -> list[Path]:
    """默认预填系统「下载」目录。"""
    folders: list[Path] = []
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        folders.append(downloads)
    desktop = Path.home() / "Desktop"
    if desktop.is_dir() and not folders:
        folders.append(desktop)
    return folders
