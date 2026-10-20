"""Оркестратор: сбор → переписывание → модерация → публикация по расписанию."""
from __future__ import annotations

import logging
import os
import random
import time

import httpx

from . import sources
from .config import ChannelCfg, Settings
from .rewriter import BudgetExceeded, finalize
from .schedule import day_start_ts, next_slot
from .telegram import Bot, TelegramError

log = logging.getLogger(__name__)
QUEUE_MAX_AGE_HOURS = 12


class Engine:
    def __init__(self, settings: Settings, channels: dict[str, ChannelCfg], storage, rewriter,
                 bot_factory=Bot, rng: random.Random | None = None):
        self.s = settings
        self.channels = channels
        self.st = storage
        self.rw = rewriter
        self.bot_factory = bot_factory
        self.rng = rng or random.Random()
        self._bots: dict[str, Bot] = {}

    # --- инфраструктура ---
    def bot(self, token: str) -> Bot:
        if token not in self._bots:
            self._bots[token] = self.bot_factory(token)
        return self._bots[token]

    def alert(self, msg: str):
        log.error("ALERT: %s", msg)
        token = os.environ.get(self.s.alert_bot_token_env or "")
        chat = os.environ.get(self.s.alert_chat_id_env or "")
        if token and chat and not self.s.dry_run:
            try:
                self.bot(token).call("sendMessage", chat_id=chat, text=f"⚠️ {msg}")
            except Exception as e:
                log.error("alert failed: %s", e)

    def paused(self, ch: ChannelCfg) -> str | None:
        return self.st.get_state(ch.id, "paused")

    def pause(self, ch: ChannelCfg, reason: str):
        self.st.set_state(ch.id, "paused", reason)
        self.alert(f"Канал {ch.id} поставлен на паузу: {reason}")

    # --- шаги ---
    def collect(self, ch: ChannelCfg) -> int:
        n = sources.collect(ch, self.st)
        self.st.set_state(ch.id, "last_collect", str(time.time()))
        log.info("[%s] collected %d new items", ch.id, n)
        return n

    def prepare(self, ch: ChannelCfg) -> int:
        """Дописывает очередь до queue_target готовыми постами."""
        self.st.expire_queue(ch.id, QUEUE_MAX_AGE_HOURS)
        ready = self.st.count_posts(ch.id, "queued") + self.st.count_posts(ch.id, "pending_review")
        made = 0
        attempts = 0
        for item in self.st.candidates(ch.id, limit=ch.queue_target * 4):
            if ready >= ch.queue_target or attempts >= ch.queue_target * 3:
                break
            attempts += 1
            item = dict(item)
            full = ""
            if item.get("fetch_full_text") and item.get("url"):
                with httpx.Client(headers={"User-Agent": sources.UA}, timeout=20, follow_redirects=True) as c:
                    full = sources.fetch_full_text(c, item["url"])
            try:
                draft = self.rw.rewrite(ch, item, full)
            except BudgetExceeded:
                log.warning("LLM budget reached, skipping prepare")
                return made
            except Exception as e:
                log.warning("[%s] rewrite failed: %s", ch.id, e)
                continue
            if not draft or not draft.suitable or not draft.text.strip():
                self.st.set_item_status(item["id"], "rejected", draft.reason if draft else "refusal")
                continue
            text = finalize(ch, draft.text, item.get("url"))
            status = "pending_review" if ch.mode == "review" else "queued"
            post_id = self.st.add_post(ch.id, text, status, item_id=item["id"], image=item.get("image"))
            self.st.set_item_status(item["id"], "used")
            if status == "pending_review":
                self.send_for_review(ch, post_id, text, item.get("image"))
            ready += 1
            made += 1
        return made

    def send_for_review(self, ch: ChannelCfg, post_id: int, text: str, image: str | None):
        if self.s.dry_run or not ch.review_chat_id or not ch.bot_token:
            log.info("[%s] post %d awaits review (no review chat / dry run)", ch.id, post_id)
            return
        kb = {"inline_keyboard": [[{"text": "✅ В очередь", "callback_data": f"ok:{post_id}"},
                                   {"text": "❌ Отклонить", "callback_data": f"no:{post_id}"}]]}
        try:
            self.bot(ch.bot_token).send_post(ch.review_chat_id, f"[{ch.title}] #{post_id}\n\n{text}",
                                             image, reply_markup=kb)
        except TelegramError as e:
            log.warning("[%s] review send failed: %s", ch.id, e)

    def poll_reviews(self):
        """Читает нажатия кнопок модерации. Один getUpdates на уникальный токен."""
        if self.s.dry_run:
            return
        tokens = {c.bot_token for c in self.channels.values()
                  if c.enabled and c.mode == "review" and c.bot_token and c.review_chat_id}
        for token in tokens:
            key = f"offset:{token[-8:]}"
            offset = self.st.get_state("_bots", key)
            try:
                updates = self.bot(token).get_updates(int(offset) if offset else None)
            except TelegramError as e:
                log.warning("getUpdates failed: %s", e)
                continue
            for u in updates:
                self.st.set_state("_bots", key, str(u["update_id"] + 1))
                cb = u.get("callback_query")
                if not cb or ":" not in cb.get("data", ""):
                    continue
                action, pid = cb["data"].split(":", 1)
                post = self.st.get_post(int(pid)) if pid.isdigit() else None
                ch = self.channels.get(post["channel"]) if post else None
                msg = cb.get("message", {})
                # принимаем решения только из чата модерации этого канала
                if not ch or str(msg.get("chat", {}).get("id")) != str(ch.review_chat_id) \
                        or post["status"] != "pending_review":
                    self.bot(token).answer_callback(cb["id"], "Неактуально")
                    continue
                new = "queued" if action == "ok" else "rejected"
                self.st.set_post_status(post["id"], new)
                self.bot(token).answer_callback(cb["id"], "В очереди" if new == "queued" else "Отклонено")
                self.bot(token).edit_markup(msg["chat"]["id"], msg["message_id"])

    def promo_due(self, ch: ChannelCfg, now: float) -> bool:
        p = ch.promo
        if not p.enabled or not p.partners:
            return False
        last = self.st.last_published_ts(ch.id, kind="promo") or 0
        # промо не чаще раза в every_days и не раньше, чем выйдет хотя бы 5 обычных постов
        return now - last >= p.every_days * 86400 and self.st.published_since(ch.id, last, "news") >= 5

    def publish_due(self, ch: ChannelCfg, now: float | None = None) -> int | None:
        now = now or time.time()
        if self.paused(ch):
            return None
        next_at = self.st.get_state(ch.id, "next_at")
        if next_at is None:
            self.st.set_state(ch.id, "next_at", str(next_slot(now, ch.schedule, self.rng)))
            return None
        if now < float(next_at):
            return None
        if self.st.published_since(ch.id, day_start_ts(now, ch.schedule)) >= ch.schedule.posts_per_day:
            self.st.set_state(ch.id, "next_at", str(next_slot(now, ch.schedule, self.rng)))
            return None

        post = None
        if self.promo_due(ch, now):
            partner = self.channels[self.rng.choice(ch.promo.partners)]
            try:
                text = self.rw.promo(ch, partner)
            except Exception as e:
                log.warning("[%s] promo failed: %s", ch.id, e)
                text = None
            if text:
                pid = self.st.add_post(ch.id, text, "queued", kind="promo")
                post = self.st.get_post(pid)
        post = post or self.st.next_queued(ch.id)
        if not post:
            return None  # очередь пуста: опубликуем, как только появится пост

        msg_id = self.send(ch, post)
        self.st.set_state(ch.id, "next_at", str(next_slot(now, ch.schedule, self.rng)))
        return msg_id

    def send(self, ch: ChannelCfg, post) -> int | None:
        if self.s.dry_run:
            log.info("[%s] DRY RUN post %d:\n%s", ch.id, post["id"], post["text"])
            self.st.set_post_status(post["id"], "published")
            return 0
        if not ch.bot_token or not ch.chat_id:
            self.pause(ch, f"нет {ch.bot_token_env} или {ch.chat_id_env} в окружении")
            return None
        try:
            mid = self.bot(ch.bot_token).send_post(ch.chat_id, post["text"], post["image"])
        except TelegramError as e:
            self.st.set_post_status(post["id"], "failed", error=str(e))
            if e.fatal:
                self.pause(ch, str(e))
            return None
        except Exception as e:
            self.st.set_post_status(post["id"], "queued", error=str(e))  # сеть: повторим позже
            log.warning("[%s] send failed: %s", ch.id, e)
            return None
        self.st.set_post_status(post["id"], "published", message_id=mid)
        log.info("[%s] published post %d as message %s", ch.id, post["id"], mid)
        return mid

    def tick(self, now: float | None = None):
        now = now or time.time()
        for ch in self.channels.values():
            if not ch.enabled or self.paused(ch):
                continue
            last = float(self.st.get_state(ch.id, "last_collect", "0"))
            if now - last >= self.s.collect_every_minutes * 60:
                try:
                    self.collect(ch)
                    self.prepare(ch)
                except Exception as e:
                    log.exception("[%s] collect/prepare crashed: %s", ch.id, e)
        try:
            self.poll_reviews()
        except Exception as e:
            log.exception("review poll crashed: %s", e)
        for ch in self.channels.values():
            if ch.enabled:
                try:
                    self.publish_due(ch, now)
                except Exception as e:
                    log.exception("[%s] publish crashed: %s", ch.id, e)

    def run_forever(self):
        log.info("engine started: %d channels, dry_run=%s", len(self.channels), self.s.dry_run)
        while True:
            self.tick()
            time.sleep(self.s.tick_seconds)
