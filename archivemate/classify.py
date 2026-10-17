"""按「日期 + 类型」生成归档路径。

组合结果示例：2026/10/ppt/数学课件.pptx
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

#: 扩展名 -> 类型目录名。未列出的扩展名直接用它自己当目录名。
TYPE_DIR_MAP: dict[str, str] = {
    "pdf": "pdf",
    "ppt": "ppt", "pptx": "ppt", "pps": "ppt", "ppsx": "ppt", "odp": "ppt",
    "doc": "doc", "docx": "doc", "odt": "doc", "rtf": "doc", "wps": "doc",
    "xls": "excel", "xlsx": "excel", "xlsm": "excel", "csv": "excel", "ods": "excel",
    "txt": "txt", "md": "txt", "log": "txt",
    "zip": "archive", "rar": "archive", "7z": "archive",
    "tar": "archive", "gz": "archive", "tgz": "archive",
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "bmp": "image", "webp": "image", "heic": "image", "svg": "image",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video", "wmv": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "flac": "audio", "aac": "audio",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DOTS_SPACES = re.compile(r"^[. ]+|[. ]+$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

MAX_STEM_LENGTH = 120


def normalize_extension(ext: str) -> str:
    text = (ext or "").strip().lower().lstrip("*").strip()
    if not text:
        return ""
    return text if text.startswith(".") else "." + text


def type_dir_for(extension: str) -> str:
    """'.pptx' -> 'ppt'。

    也接受完整文件名（'数学课件.pptx'）；未收录的扩展名回退为扩展名本身，
    完全没有扩展名时归到 'other'。
    """
    text = (extension or "").strip()
    if text and not text.startswith("."):
        text = Path(text).suffix
    ext = normalize_extension(text).lstrip(".")
    if not ext:
        return "other"
    mapped = TYPE_DIR_MAP.get(ext)
    if mapped:
        return mapped
    cleaned = _ILLEGAL.sub("", ext)
    return cleaned or "other"


def sanitize_filename(name: str) -> str:
    """清掉跨平台非法字符，并处理 Windows 保留名与超长文件名。"""
    text = unicodedata.normalize("NFC", name or "")
    text = text.replace("/", "_").replace("\\", "_")
    text = _ILLEGAL.sub("_", text)
    text = text.strip()
    if not text:
        text = "未命名文件"

    path = Path(text)
    stem = _DOTS_SPACES.sub("", path.stem) or "未命名文件"
    suffix = _ILLEGAL.sub("", path.suffix)

    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    if len(stem) > MAX_STEM_LENGTH:
        stem = stem[:MAX_STEM_LENGTH].rstrip()

    result = stem + suffix
    if len(result) > 200:
        keep = 200 - len(suffix)
        result = stem[:max(1, keep)].rstrip() + suffix
    return result


def resolve_date(source: Path, *, date_source: str = "mtime",
                 now: datetime | None = None) -> datetime:
    """决定归档用哪个日期。默认用文件修改时间（更接近「什么时候收到的」）。"""
    if date_source == "now":
        return now or datetime.now()
    try:
        stamp = source.stat().st_mtime
    except OSError:
        return now or datetime.now()
    return datetime.fromtimestamp(stamp)


def build_dest_rel(source: Path, *, date_source: str = "mtime",
                   now: datetime | None = None) -> str:
    """返回仓库内的相对路径，例如 '2026/10/ppt/数学课件.pptx'。"""
    source = Path(source)
    when = resolve_date(source, date_source=date_source, now=now)
    type_dir = type_dir_for(source.suffix)
    filename = sanitize_filename(source.name)
    return f"{when:%Y}/{when:%m}/{type_dir}/{filename}"


def dest_parts(dest_rel: str) -> tuple[str, str, str, str]:
    """把相对路径拆成 (年, 月, 类型, 文件名)。"""
    parts = dest_rel.replace("\\", "/").split("/")
    if len(parts) == 4:
        return (parts[0], parts[1], parts[2], parts[3])
    if len(parts) == 1:
        return ("", "", "", parts[0])
    return (parts[0], parts[1] if len(parts) > 1 else "", "", parts[-1])


def human_category(dest_rel: str) -> str:
    year, month, type_dir, _name = dest_parts(dest_rel)
    if year and month:
        return f"{year} 年 {int(month)} 月 / {type_dir}"
    return type_dir or "未分类"
