"""Когда публиковать: равномерно по активным часам, со случайным разбросом, без ночных постов."""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import ScheduleCfg


def in_active_hours(dt: datetime, s: ScheduleCfg) -> bool:
    start, end = s.active_hours
    return start <= dt.hour < end


def next_slot(now_ts: float, s: ScheduleCfg, rng: random.Random | None = None) -> float:
    """Время следующего поста: средний интервал = активное окно / posts_per_day, ± jitter."""
    rng = rng or random.Random()
    tz = ZoneInfo(s.timezone)
    start, end = s.active_hours
    window_min = (end - start) * 60
    base = window_min / s.posts_per_day
    gap = max(s.min_gap_minutes, base * rng.uniform(1 - s.jitter, 1 + s.jitter))
    t = datetime.fromtimestamp(now_ts, tz) + timedelta(minutes=gap)
    if not in_active_hours(t, s):
        day = t + timedelta(days=1) if t.hour >= end else t
        morning = day.replace(hour=start, minute=0, second=0, microsecond=0)
        # первый пост дня тоже в случайное время, чтобы не было «ровно в 8:00»
        t = morning + timedelta(minutes=rng.uniform(5, max(10, base * 0.5)))
    return t.timestamp()


def day_start_ts(now_ts: float, s: ScheduleCfg) -> float:
    tz = ZoneInfo(s.timezone)
    d = datetime.fromtimestamp(now_ts, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()
