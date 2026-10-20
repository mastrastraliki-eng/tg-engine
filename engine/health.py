"""Ежедневный самоконтроль: короткий отчёт владельцу в личку от бота уведомлений, без вызовов LLM."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Kyiv")
REPORT_HOUR = 9


class ErrorCounter(logging.Handler):
    """Считает ошибки в журнале с момента прошлого отчёта (в памяти; после перезапуска с нуля)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.last = ""
        self.started = time.time()

    def emit(self, record):
        self.count += 1
        self.last = record.getMessage()[:150]

    def reset(self):
        self.count, self.last = 0, ""


errors = ErrorCounter()


def install():
    if errors not in logging.getLogger().handlers:
        logging.getLogger().addHandler(errors)


def report_due(st, now: float) -> bool:
    dt = datetime.fromtimestamp(now, TZ)
    return dt.hour >= REPORT_HOUR and st.get_state("_health", "last_day") != dt.strftime("%Y-%m-%d")


def _check_keys(client: httpx.Client) -> list[str]:
    """Бесплатные запросы: список моделей Anthropic и проверка токена Cloudflare."""
    bad = []
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        try:
            r = client.get("https://api.anthropic.com/v1/models", params={"limit": 1},
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            if r.status_code != 200:
                bad.append(f"ключ Anthropic не отвечает ({r.status_code})")
        except httpx.HTTPError as e:
            bad.append(f"Anthropic недоступен ({type(e).__name__})")
    else:
        bad.append("нет ключа Anthropic")
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if token:
        try:
            r = client.get("https://api.cloudflare.com/client/v4/user/tokens/verify",
                           headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200 or not r.json().get("success"):
                bad.append(f"ключ Cloudflare не работает ({r.status_code})")
        except (httpx.HTTPError, ValueError) as e:
            bad.append(f"Cloudflare недоступен ({type(e).__name__})")
    return bad


def build_report(eng, now: float | None = None, client: httpx.Client | None = None) -> str:
    now = now or time.time()
    st = eng.st
    problems: list[str] = []
    lines: list[str] = []
    for ch in eng.channels.values():
        if not ch.enabled:
            continue
        name = f"@{ch.username}" if ch.username else ch.id
        posts = st.published_since(ch.id, now - 86400)
        subs = None
        if ch.bot_token and ch.chat_id:
            try:
                subs = eng.bot(ch.bot_token).call("getChatMemberCount", chat_id=ch.chat_id)
            except Exception as e:
                problems.append(f"{name}: бот не видит канал ({e})")
        prev = st.get_state(ch.id, "health_subs")
        delta = ""
        if subs is not None:
            if prev is not None:
                d = subs - int(prev)
                delta = f" ({d:+d})"
            st.set_state(ch.id, "health_subs", str(subs))
        lines.append(f"{name}: постов за сутки {posts}, подписчиков {subs if subs is not None else '?'}{delta}")
        if eng.paused(ch):
            problems.append(f"{name} на паузе: {eng.paused(ch)}")
        if posts == 0:
            problems.append(f"{name}: за сутки ни одного поста")
    close = client is None
    client = client or httpx.Client(timeout=20)
    try:
        problems += _check_keys(client)
    finally:
        if close:
            client.close()
    used, limit = st.llm_calls_last_day(), eng.s.max_llm_calls_per_day
    if used >= limit * 0.9:
        problems.append(f"лимит запросов к Claude почти исчерпан: {used}/{limit}")
    if errors.count:
        problems.append(f"ошибок в журнале: {errors.count}, последняя: {errors.last}")
    head = "✅ Всё работает" if not problems else "⚠ Есть проблемы"
    text = "\n".join([f"{head} ({datetime.fromtimestamp(now, TZ):%d.%m %H:%M})", *lines])
    if problems:
        text += "\n\n" + "\n".join(f"• {p}" for p in problems)
    return text


def daily(eng, now: float | None = None) -> bool:
    """Шлёт отчёт раз в день после 9:00 по Киеву. True, если отправлен."""
    now = now or time.time()
    if not report_due(eng.st, now):
        return False
    eng.st.set_state("_health", "last_day", datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d"))
    send(eng, build_report(eng, now))
    errors.reset()
    return True


def send(eng, text: str):
    token = os.environ.get(eng.s.alert_bot_token_env or "")
    chat = os.environ.get(eng.s.alert_chat_id_env or "")
    log.info("health report:\n%s", text)
    if token and chat and not eng.s.dry_run:
        try:
            eng.bot(token).call("sendMessage", chat_id=chat, text=text, link_preview_options={"is_disabled": True})
        except Exception as e:
            log.warning("health report not sent: %s", e)
