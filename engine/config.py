"""Конфиги: глобальные настройки + по одному YAML на канал."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class SourceCfg(BaseModel):
    type: Literal["rss", "reddit"]
    url: str | None = None          # для rss
    subreddit: str | None = None    # для reddit
    name: str | None = None
    weight: float = 1.0             # приоритет источника при отборе
    use_images: bool = False        # брать картинку из источника (следи за правами)
    fetch_full_text: bool = False   # скачивать страницу статьи для контекста

    @model_validator(mode="after")
    def _check(self):
        if self.type == "rss" and not self.url:
            raise ValueError("rss source needs url")
        if self.type == "reddit" and not self.subreddit:
            raise ValueError("reddit source needs subreddit")
        if not self.name:
            self.name = self.url if self.type == "rss" else f"r/{self.subreddit}"
        return self


class ScheduleCfg(BaseModel):
    posts_per_day: int = Field(6, ge=1, le=48)
    active_hours: tuple[int, int] = (8, 23)   # [начало, конец) по локальному времени
    timezone: str = "Europe/Kyiv"
    min_gap_minutes: int = 45
    jitter: float = Field(0.3, ge=0, le=0.9)  # разброс интервала ±30%


class StyleCfg(BaseModel):
    language: str = "uk"
    voice: str = "Коротко, по ділу, без води."
    rules: list[str] = []
    max_chars: int = Field(900, ge=200, le=3500)
    hashtags: int = Field(0, ge=0, le=5)
    include_source_link: bool = True
    signature: str = ""             # например "@my_channel"


class FilterCfg(BaseModel):
    include_keywords: list[str] = []   # если не пусто, нужен хотя бы один
    exclude_keywords: list[str] = []
    max_age_hours: int = 36
    min_title_len: int = 15


class PromoCfg(BaseModel):
    enabled: bool = False
    every_days: float = 5               # не чаще одного промо-поста раз в N дней
    partners: list[str] = []            # id других каналов сети


class ChannelCfg(BaseModel):
    id: str
    title: str
    enabled: bool = True
    username: str = ""                  # @handle, нужен для взаимного пиара
    niche: str
    bot_token_env: str                  # имя переменной окружения с токеном бота
    chat_id_env: str                    # имя переменной окружения с id/@username канала
    mode: Literal["auto", "review"] = "review"
    review_chat_id_env: str | None = None
    sources: list[SourceCfg]
    schedule: ScheduleCfg = ScheduleCfg()
    style: StyleCfg = StyleCfg()
    filters: FilterCfg = FilterCfg()
    promo: PromoCfg = PromoCfg()
    queue_target: int = 3               # сколько готовых постов держать в очереди

    @property
    def bot_token(self) -> str | None:
        return os.environ.get(self.bot_token_env)

    @property
    def chat_id(self) -> str | None:
        return os.environ.get(self.chat_id_env)

    @property
    def review_chat_id(self) -> str | None:
        return os.environ.get(self.review_chat_id_env) if self.review_chat_id_env else None


class Settings(BaseModel):
    db_path: str = "data/engine.db"
    channels_dir: str = "channels"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_llm_calls_per_day: int = 300
    tick_seconds: int = 60
    collect_every_minutes: int = 30
    dry_run: bool = False               # ничего не отправлять в Telegram
    alert_chat_id_env: str | None = "ALERT_CHAT_ID"
    alert_bot_token_env: str | None = "ALERT_BOT_TOKEN"


def load_settings(path: str | Path = "settings.yaml") -> Settings:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    s = Settings(**(data or {}))
    if os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"):
        s.dry_run = True
    return s


def load_channels(directory: str | Path) -> dict[str, ChannelCfg]:
    channels: dict[str, ChannelCfg] = {}
    for f in sorted(Path(directory).glob("*.yaml")):
        cfg = ChannelCfg(**yaml.safe_load(f.read_text(encoding="utf-8")))
        if cfg.id in channels:
            raise ValueError(f"duplicate channel id {cfg.id} in {f}")
        channels[cfg.id] = cfg
    for cfg in channels.values():
        for p in cfg.promo.partners:
            if p not in channels:
                raise ValueError(f"{cfg.id}: unknown promo partner {p}")
    return channels
