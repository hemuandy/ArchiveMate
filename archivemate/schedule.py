"""定时同步的时刻计算。

只做一件事：给定「每天 / 每周 / 每月」三种档位，算出下一次该在什么时候同步。
时间一律用本机时区的本地时间，零点 = 00:00:00。
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: 档位 -> 界面文案。顺序即下拉菜单顺序。
SCHEDULE_CHOICES: tuple[tuple[str, str], ...] = (
    ("off", "关闭"),
    ("daily", "每天 0 点"),
    ("weekly", "每周一 0 点"),
    ("monthly", "每月 1 号 0 点"),
)

SCHEDULE_KEYS: tuple[str, ...] = tuple(key for key, _ in SCHEDULE_CHOICES)


def schedule_label(schedule: str) -> str:
    for key, label in SCHEDULE_CHOICES:
        if key == schedule:
            return label
    return SCHEDULE_CHOICES[0][1]


def next_run(schedule: str, now: datetime | None = None) -> datetime | None:
    """返回下一次同步的时刻；关闭时返回 None。

    返回的时刻严格晚于 now —— 正好在 0 点整调用时会给出「下一个」零点，
    避免同一次触发被重复执行。
    """
    if schedule not in ("daily", "weekly", "monthly"):
        return None
    current = (now or datetime.now()).replace(microsecond=0)
    today = current.replace(hour=0, minute=0, second=0)

    if schedule == "daily":
        target = today + timedelta(days=1)
    elif schedule == "weekly":
        # weekday(): 周一 = 0
        days = (7 - current.weekday()) % 7
        target = today + timedelta(days=days)
        if target <= current:
            target += timedelta(days=7)
    else:
        # 每月 1 号
        if current.month == 12:
            target = today.replace(year=current.year + 1, month=1)
        else:
            target = today.replace(month=current.month + 1)
        target = target.replace(day=1)
        if target <= current:
            if target.month == 12:
                target = target.replace(year=target.year + 1, month=1, day=1)
            else:
                target = target.replace(month=target.month + 1, day=1)

    return target


def describe_next(schedule: str, now: datetime | None = None) -> str:
    """给界面用的一句话，例如「下次同步：明天 00:00（每天 0 点）」。"""
    if schedule not in ("daily", "weekly", "monthly"):
        return "定时同步已关闭"
    moment = next_run(schedule, now)
    if moment is None:
        return "定时同步已关闭"
    return f"下次同步：{moment.strftime('%Y-%m-%d %H:%M')}（{schedule_label(schedule)}）"
