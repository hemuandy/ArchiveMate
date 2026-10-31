"""按「学年 / 学期 / 类型 / 科目」生成归档路径。

组合结果示例：``2026-2027学年/上学期/课件/数学/数学课件.pptx``

四层规则（v2.0.0 起）：

* 学年 —— 由文件修改时间推算。9 月开学，9 月到次年 1 月算**上学期**，
  2 月到 8 月算**下学期**，两者同属一个学年，例如 2026 年 10 月与
  2027 年 1 月都落在 ``2026-2027学年``。
* 学期 —— ``上学期`` / ``下学期``。
* 类型 —— ``.ppt`` / ``.pptx`` 归 ``课件``，其余全部归 ``其他``。
* 科目 —— 语文/数学/英语/物理/化学/生物/地理/政治/历史，认不出就是 ``其他``。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

#: 课件目录只认这两种扩展名，其余（含 .pps/.odp）都算「其他」。
PRESENTATION_EXTENSIONS = frozenset({".ppt", ".pptx"})

TYPE_SLIDE = "课件"
TYPE_OTHER = "其他"
TYPE_DIRS: tuple[str, ...] = (TYPE_SLIDE, TYPE_OTHER)

TERM_FIRST = "上学期"
TERM_SECOND = "下学期"

OTHER_SUBJECT = "其他"
#: 固定 9 科，顺序也决定了同分时的优先次序。
SUBJECTS: tuple[str, ...] = (
    "语文", "数学", "英语", "物理", "化学", "生物", "地理", "政治", "历史",
)
SUBJECT_DIRS: tuple[str, ...] = SUBJECTS + (OTHER_SUBJECT,)

#: 科目词库。多字词是主要依据；单字别名只在「独立成词」时才算数
#: （见 _single_char_hit），否则「学生手册」会被认成生物。
SUBJECT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "语文": ("语文", "汉语", "作文", "文言文", "古诗", "古文", "默写", "名著",
             "病句", "修辞", "议论文", "记叙文", "说明文", "诗歌", "拼音",
             "字词", "语基", "写作", "阅读"),
    "数学": ("数学", "代数", "几何", "函数", "方程", "不等式", "三角", "数列",
             "概率", "统计", "导数", "微积分", "算术"),
    "英语": ("英语", "英文", "听力", "完形", "语法", "单词", "词汇", "口语",
             "english", "grammar", "listening", "unit"),
    "物理": ("物理", "力学", "电学", "电磁学", "光学", "热学", "运动学",
             "电路", "浮力", "压强"),
    "化学": ("化学", "有机化学", "无机化学", "方程式", "元素周期", "溶液",
             "氧化还原", "摩尔"),
    "生物": ("生物", "细胞", "遗传", "生态", "光合作用", "呼吸作用", "孟德尔",
             "基因", "细胞分裂"),
    "地理": ("地理", "地图", "气候", "洋流", "地质", "等高线", "时区", "经纬度"),
    "政治": ("政治", "道德与法治", "法治", "思想政治", "时政", "哲学",
             "经济生活", "政治生活", "法律"),
    "历史": ("历史", "史料", "近代史", "古代史", "世界史", "中国史", "朝代",
             "辛亥革命"),
}

#: 单字别名，只在独立成词时参与匹配（「数1.pdf」「生-笔记.pdf」）。
SUBJECT_ALIASES: dict[str, str] = {
    "语": "语文", "数": "数学", "英": "英语", "物": "物理", "化": "化学",
    "生": "生物", "地": "地理", "政": "政治", "史": "历史",
}

#: 题型词：不指向任何科目，匹配前先摘掉，免得干扰科目判断。
TITLE_WORDS: tuple[str, ...] = (
    "试卷", "试题", "答案", "解析", "讲义", "作业", "练习", "笔记", "复习",
    "提纲", "课件", "真题", "模拟", "单元", "期中", "期末", "月考", "总结",
    "错题", "学案", "教案",
)

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DOTS_SPACES = re.compile(r"^[. ]+|[. ]+$")
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

MAX_STEM_LENGTH = 120

__all__ = [
    "build_dest_rel", "dest_parts", "human_category", "sanitize_filename",
    "subject_for", "school_year_for", "term_for", "type_dir_for",
    "normalize_extension", "resolve_date",
    "SUBJECTS", "SUBJECT_DIRS", "SUBJECT_KEYWORDS", "SUBJECT_ALIASES",
    "TITLE_WORDS", "TYPE_DIRS", "TYPE_SLIDE", "TYPE_OTHER",
    "TERM_FIRST", "TERM_SECOND", "OTHER_SUBJECT", "PRESENTATION_EXTENSIONS",
]


# ----------------------------------------------------------------------
# 单层规则
# ----------------------------------------------------------------------

def normalize_extension(ext: str) -> str:
    text = (ext or "").strip().lower().lstrip("*").strip()
    if not text:
        return ""
    return text if text.startswith(".") else "." + text


def type_dir_for(extension: str) -> str:
    """'.pptx' -> '课件'，其它一切（含 .doc/.pdf/无扩展名）-> '其他'。

    也接受完整文件名（'数学课件.pptx'）。
    """
    text = (extension or "").strip()
    if text and not text.startswith("."):
        text = Path(text).suffix
    ext = normalize_extension(text)
    return TYPE_SLIDE if ext in PRESENTATION_EXTENSIONS else TYPE_OTHER


def school_year_for(when: datetime) -> str:
    """返回 '2026-2027' 这样的学年区间。

    9 月开学：9 月～12 月属于当年开始的学年；1 月仍是上一学年（上学期）；
    2 月～8 月是上一学年开始的那个学年的下学期。
    """
    year, month = when.year, when.month
    start = year if month >= 9 else year - 1
    return f"{start}-{start + 1}"


def term_for(when: datetime) -> str:
    """9 月～次年 1 月是上学期，2 月～8 月是下学期。"""
    return TERM_FIRST if when.month >= 9 or when.month == 1 else TERM_SECOND


def _strip_title_words(text: str) -> str:
    for word in TITLE_WORDS:
        text = text.replace(word, " ")
    return text


def _single_char_hit(text: str, char: str) -> bool:
    """单字别名要求左右都不是汉字，避免「学生手册」被认成生物。"""
    start = 0
    while True:
        index = text.find(char, start)
        if index < 0:
            return False
        before = text[index - 1] if index > 0 else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if not _CJK.match(before or " ") and not _CJK.match(after or " "):
            return True
        start = index + 1


def subject_for(filename: str) -> str:
    """认科目。认不出来（或认出了多个打平）就按规则选一个，兜底 '其他'。

    冲突处理的核心是「先比词长，再比出现位置，最后比 SUBJECTS 顺序」：

    * 「生物」含「物」，但多字词先于单字别名，所以不会被认成物理；
    * 两个词同长且同位置时，按 SUBJECTS 里的先后定胜负。
    """
    text = unicodedata.normalize("NFC", filename or "").lower()
    stem = _strip_title_words(text)
    if not stem.strip():
        return OTHER_SUBJECT

    best_subject = ""
    best_rank: tuple[int, int, int] | None = None   # (-词长, 位置, 科次序)
    for order, subject in enumerate(SUBJECTS):
        for word in SUBJECT_KEYWORDS[subject]:
            index = stem.find(word.lower())
            if index < 0:
                continue
            rank = (-len(word), index, order)
            if best_rank is None or rank < best_rank:
                best_rank, best_subject = rank, subject
    if best_subject:
        return best_subject

    for order, (alias, subject) in enumerate(SUBJECT_ALIASES.items()):
        if not _single_char_hit(stem, alias):
            continue
        rank = (-1, stem.find(alias), order)
        if best_rank is None or rank < best_rank:
            best_rank, best_subject = rank, subject
    return best_subject or OTHER_SUBJECT


# ----------------------------------------------------------------------
# 文件名清洗
# ----------------------------------------------------------------------

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


# ----------------------------------------------------------------------
# 组合
# ----------------------------------------------------------------------

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
    """返回仓库内的相对路径，例如 '2026-2027学年/上学期/课件/数学/数学课件.pptx'。"""
    source = Path(source)
    when = resolve_date(source, date_source=date_source, now=now)
    filename = sanitize_filename(source.name)
    return "/".join((
        f"{school_year_for(when)}学年",
        term_for(when),
        type_dir_for(source.suffix),
        subject_for(source.stem),
        filename,
    ))


def dest_parts(dest_rel: str) -> tuple[str, str, str, str, str]:
    """把相对路径拆成 (学年, 学期, 类型, 科目, 文件名)。"""
    parts = dest_rel.replace("\\", "/").split("/")
    if len(parts) >= 5:
        return (parts[0], parts[1], parts[2], parts[3], "/".join(parts[4:]))
    padded = parts + [""] * (5 - len(parts))
    return (padded[0], padded[1], padded[2], padded[3], padded[4])


def human_category(dest_rel: str) -> str:
    """给界面用的一句话分类，例如 '2026-2027学年 上学期 / 课件 / 数学'。"""
    year, term, type_dir, subject, _name = dest_parts(dest_rel)
    pieces = [piece for piece in (f"{year} {term}".strip(), type_dir, subject) if piece]
    return " / ".join(pieces) or "未分类"
