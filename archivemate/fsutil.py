"""文件系统小工具：体积格式化、稳定性判定、无冲突移动。"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .errors import FileTooLarge

MiB = 1024 * 1024
GiB = 1024 * MiB

WARN_BYTES = 50 * MiB
BLOCK_BYTES = 90 * MiB
BATCH_BYTES = 2 * GiB


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def describe_size_limit(size: int) -> str:
    """给用户看的一句话体积说明。"""
    if size >= BLOCK_BYTES:
        return (f"文件 {human_size(size)} 超过 {human_size(BLOCK_BYTES)} 上限，"
                f"已阻止推送；建议先压缩，或改用 Git LFS。")
    if size >= WARN_BYTES:
        return (f"文件 {human_size(size)} 超过 {human_size(WARN_BYTES)}，"
                f"推送会比较慢，且仓库体积增长明显。")
    return ""


def check_size(size: int, *, hard: bool = True) -> None:
    """hard=True 时超过阻塞阈值直接抛错。"""
    if hard and size >= BLOCK_BYTES:
        raise FileTooLarge(describe_size_limit(size))


def file_signature(path: Path) -> tuple[int, float] | None:
    """(size, mtime)；文件不存在或不可读时返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime)


def is_stable(path: Path, *, settle_seconds: float = 3.0) -> bool:
    """文件是否已经写完：两次采样之间大小与修改时间都没变。

    settle_seconds 是两次采样之间要求的最短间隔；调用方负责轮询。
    """
    first = file_signature(path)
    if first is None:
        return False
    if first[0] == 0:
        return False
    time.sleep(min(settle_seconds, 0.4))
    second = file_signature(path)
    return second is not None and first == second


def unique_path(directory: Path, filename: str) -> Path:
    """在 directory 下找一个不冲突的文件名：name.ext, name (2).ext, ..."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 10000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise OSError(f"无法为 {filename} 找到可用的文件名")


def safe_move(src: Path, dest_dir: Path, filename: str) -> Path:
    """把 src 移动进 dest_dir，跨盘自动降级为复制 + 删除。"""
    target = unique_path(dest_dir, filename)
    try:
        shutil.move(str(src), str(target))
    except OSError:
        # 跨卷时 shutil.move 内部已处理，这里兜底 copy+unlink
        shutil.copy2(str(src), str(target))
        try:
            src.unlink()
        except OSError:
            pass
    return target


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def directory_size(path: Path) -> tuple[int, int]:
    """(总字节数, 文件个数)。"""
    total = 0
    count = 0
    if not path.exists():
        return (0, 0)
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
                count += 1
            except OSError:
                continue
    return (total, count)


def clear_directory(path: Path) -> tuple[int, int]:
    """删除目录内容但保留目录本身，返回 (删除文件数, 释放字节数)。"""
    removed = 0
    freed = 0
    if not path.exists():
        return (0, 0)
    for entry in list(path.iterdir()):
        try:
            if entry.is_dir() and not entry.is_symlink():
                size, count = directory_size(entry)
                shutil.rmtree(entry)
                freed += size
                removed += count
            else:
                size = entry.stat().st_size
                entry.unlink()
                freed += size
                removed += 1
        except OSError:
            continue
    return (removed, freed)
