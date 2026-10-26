"""Оркестратор: сбор → переписывание → модерация → публикация по расписанию."""
from __future__ import annotations

import html
import logging
import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import cards, health, market, sources
from .config import ChannelCfg, Settings
from .rewriter import BudgetExceeded, channel_link, finalize
from .schedule import day_start_ts, in_active_hours, next_slot
from .telegram import Bot, TelegramError

log = logging.getLogger(__name__)
QUEUE_MAX_AGE_HOURS = 12
SEED_GAP_MINUTES = 12


def headline(text: str) -> str:
    """Заголовок поста: первая содержательная строка, а не короткая рубрика вроде «⚡️ Терміново»."""
    lines = [l.strip() for l in re.sub(r"<[^>]+>", "", text or "").split("\n") if l.strip()]
    for l in lines:
        if len(re.findall(r"\w{2,}", l)) >= 3:
            return l
    return lines[0] if lines else ""


def _stems(text: str) -> set[str]:
    return {w[:5] for w in sources.normalize_title(text).split() if len(w) > 3}


def is_repeat(headline: str, recent: list[str], threshold: float = 0.6) -> bool:
    """Тот же сюжет, что у недавнего поста: совпадает большинство основ слов заголовка."""
    a = _stems(headline)
    if len(a) < 3:
        return False
    for r in recent:
        b = _stems(r)
        if len(b) >= 3 and len(a & b) / min(len(a), len(b)) >= threshold:
            return True
    return False


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

    def images_for(self, ch: ChannelCfg, title: str, source_img: str | None) -> tuple[str | None, str | None]:
        """(картинка, запасная картинка) по режиму канала."""
        mode = ch.style.images
        if mode == "none":
            return None, None
        if mode == "source":
            return source_img, None
        try:
            card = cards.title_card(title, ch.style.card_label or ch.title, ch.style.card_color,
                                    ch.style.signature, self.s.cards_dir, ch.schedule.timezone)
        except Exception as e:  # картинка не должна ронять пост
            log.warning("[%s] card failed: %s", ch.id, e)
            card = None
        if mode == "card" or not source_img:
            return card, None
        return source_img, card

    def ai_image(self, prompt: str, style: str = "") -> str | None:
        """ИИ-иллюстрация с дневным лимитом, чтобы не разориться."""
        from datetime import date
        from . import imagegen
        k = f"ai_images_{date.today().isoformat()}"
        used = int(self.st.get_state("_global", k, "0"))
        if not prompt or used >= self.s.max_ai_images_per_day:
            return None
        path = imagegen.generate(prompt, self.s.image_model, self.s.cards_dir, providers=self.s.image_providers,
                                 style=style)
        if path:
            self.st.set_state("_global", k, str(used + 1))
        return path

    def chart_image(self, ch: ChannelCfg, coin: tuple[str, str]) -> str | None:
        """Свечной график монеты за сутки; один и тот же график держим 20 минут, чтобы не дёргать API."""
        cid, sym = coin
        cached = (self.st.get_state("_charts", cid) or "").split("|", 1)
        if len(cached) == 2 and time.time() - float(cached[0]) < 1200 and os.path.exists(cached[1]):
            return cached[1]
        with httpx.Client(headers={"User-Agent": sources.UA}, timeout=20) as c:
            ohlc = market.fetch_ohlc(c, cid, days=1)
        if len(ohlc) < 10:
            return None
        path = cards.chart_card(sym, ohlc, ch.style.card_label or ch.title, ch.style.card_color,
                                self.s.cards_dir, ch.schedule.timezone)
        self.st.set_state("_charts", cid, f"{time.time()}|{path}")
        return path

    def pick_media(self, ch: ChannelCfg, draft, title: str, first: str,
                   src_img: str | None) -> tuple[str | None, str | None]:
        """Чередует виды медиа по весам media_mix, чтобы лента не была однообразной.

        Видео из источника берём всегда. Вид, который был в прошлом посте, выпадает реже.
        Если выбранный вид не получился (нет монеты, сервис картинок лёг), пробуем следующий.
        """
        mix = ch.style.media_mix
        card = self.images_for(ch, first, None)[0] if ch.style.images != "none" else None
        if src_img and src_img.startswith("video:"):
            self.st.set_state(ch.id, "last_media", "video")
            return src_img, card
        coin = market.detect_coin(f"{draft.text} {title}") if mix.get("chart") else None
        makers = {
            "source": (lambda: src_img) if src_img else None,
            "chart": (lambda: self.chart_image(ch, coin)) if coin else None,
            "ai": (lambda: self.ai_image(draft.image_prompt, ch.style.ai_style)) if draft.image_prompt else None,
            "card": (lambda: card) if card else None,
        }
        last = self.st.get_state(ch.id, "last_media")
        pool = {k: w * (0.25 if k == last else 1.0) for k, w in mix.items() if w > 0 and makers.get(k)}
        while pool:
            kind = self.rng.choices(list(pool), weights=list(pool.values()))[0]
            pool.pop(kind)
            try:
                image = makers[kind]()
            except Exception as e:  # медиа не должно ронять пост
                log.warning("[%s] media %s failed: %s", ch.id, kind, e)
                image = None
            if image:
                self.st.set_state(ch.id, "last_media", kind)
                return image, (src_img if kind != "source" and src_img else card)
        return (src_img, card) if src_img else (card, None)

    def next_format(self, ch: ChannelCfg) -> str:
        """Случайный формат подачи, но не тот же, что в двух последних постах."""
        fmts = ch.style.formats
        if not fmts:
            return ""
        recent = (self.st.get_state(ch.id, "last_formats") or "").split("|")
        pool = [i for i in range(len(fmts)) if str(i) not in recent[-2:]] or list(range(len(fmts)))
        i = self.rng.choice(pool)
        self.st.set_state(ch.id, "last_formats", "|".join((recent + [str(i)])[-2:]))
        return fmts[i]

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
        if ch.mode == "auto":  # канал сняли с модерации: ждущие кнопок посты идут в очередь
            self.st.db.execute("UPDATE posts SET status='queued' WHERE channel=? AND status='pending_review'", (ch.id,))
            self.st.db.commit()
        ready = self.st.count_posts(ch.id, "queued") + self.st.count_posts(ch.id, "pending_review")
        target = max(ch.queue_target, int(self.st.get_state(ch.id, "seed_left", "0")))
        made = 0
        attempts = 0
        recent = self.st.recent_headlines(ch.id)
        for item in self.st.candidates(ch.id, limit=target * 4):
            if ready >= target or attempts >= target * 3:
                break
            attempts += 1
            item = dict(item)
            full = ""
            if item.get("fetch_full_text") and item.get("url"):
                with httpx.Client(headers={"User-Agent": sources.UA}, timeout=20, follow_redirects=True) as c:
                    full = sources.fetch_full_text(c, item["url"])
            try:
                draft = self.rw.rewrite(ch, item, full, recent=recent,
                                        fmt=self.next_format(ch),
                                        alt_fmt=self.next_format(ch) if ch.style.editor else "")
            except BudgetExceeded:
                log.warning("LLM budget reached, skipping prepare")
                return made
            except Exception as e:
                log.warning("[%s] rewrite failed: %s", ch.id, e)
                continue
            if not draft or not draft.suitable or not draft.text.strip():
                self.st.set_item_status(item["id"], "rejected", draft.reason if draft else "refusal")
                continue
            first = headline(draft.text) or item["title"]
            if is_repeat(first, recent):
                self.st.set_item_status(item["id"], "duplicate", "same story as a recent post")
                log.info("[%s] skip repeat: %s", ch.id, first[:80])
                continue
            recent.append(first)
            text = finalize(ch, draft.text, item.get("url"))
            status = "pending_review" if ch.mode == "review" else "queued"
            src_img = item.get("image")
            if not src_img and item.get("use_images") and item.get("url") and ch.style.images in ("source", "source_or_card"):
                with httpx.Client(headers={"User-Agent": sources.UA}, timeout=15, follow_redirects=True) as c:
                    src_img = sources.fetch_og_image(c, item["url"])
            if not draft.media_ok:
                src_img = None
            if ch.style.media_mix:
                image, fallback = self.pick_media(ch, draft, item["title"], first, src_img)
            else:
                image, fallback = self.images_for(ch, first, src_img)
                if ch.style.ai_images == "always" or (ch.style.ai_images == "no_photo" and not src_img):
                    ai = self.ai_image(draft.image_prompt, ch.style.ai_style)
                    if ai:
                        image, fallback = ai, image
            post_id = self.st.add_post(ch.id, text, status, item_id=item["id"], image=image, fallback_image=fallback)
            self.st.set_item_status(item["id"], "used")
            if status == "pending_review":
                self.send_for_review(ch, post_id, text, image, fallback)
            ready += 1
            made += 1
        return made

    def send_for_review(self, ch: ChannelCfg, post_id: int, text: str, image: str | None,
                        fallback: str | None = None):
        if self.s.dry_run or not ch.review_chat_id or not ch.bot_token:
            log.info("[%s] post %d awaits review (no review chat / dry run)", ch.id, post_id)
            return
        kb = {"inline_keyboard": [[{"text": "✅ В очередь", "callback_data": f"ok:{post_id}"},
                                   {"text": "❌ Отклонить", "callback_data": f"no:{post_id}"}]]}
        try:
            self.bot(ch.bot_token).send_post(ch.review_chat_id, f"[{ch.title}] #{post_id}\n\n{text}",
                                             image, reply_markup=kb, fallback_image=fallback)
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
        # у приватного канала нет @username, рекламировать его нечем
        if not p.enabled or not any(self.channels[x].username for x in p.partners):
            return False
        last = self.st.last_published_ts(ch.id, kind="promo") or 0
        # промо не чаще раза в every_days и не раньше, чем выйдет хотя бы 5 обычных постов
        return now - last >= p.every_days * 86400 and self.st.published_since(ch.id, last, "news") >= 5

    # --- стартовое наполнение: несколько постов подряд, чтобы новый подписчик видел живую ленту ---
    def seed(self, ch: ChannelCfg, n: int, now: float | None = None):
        now = now or time.time()
        self.st.set_state(ch.id, "seed_left", str(n))
        self.st.set_state(ch.id, "seed_next", str(now))  # сразу, даже ночью: каналы только запускаются
        if ch.kind == "relay":
            # тревоги задним числом не нужны: только свежие сводки из фильтрованных источников (Генштаб)
            names = [src.name for src in ch.sources if src.include_regex]
            if not names:
                return
            marks = ",".join("?" * len(names))
            rows = self.st.db.execute(
                f"SELECT id FROM items WHERE channel=? AND source IN ({marks}) AND status='rejected' "
                "AND reason='before start' AND COALESCE(published_ts, fetched_ts)>? "
                "ORDER BY COALESCE(published_ts, fetched_ts) DESC LIMIT ?",
                (ch.id, *names, now - 86400, n)).fetchall()
            self.st.set_state(ch.id, "seed_items", ",".join(str(r["id"]) for r in reversed(rows)))

    def seed_step(self, ch: ChannelCfg, now: float) -> bool:
        """True, если сейчас время очередного стартового поста."""
        left = int(self.st.get_state(ch.id, "seed_left", "0"))
        return left > 0 and now >= float(self.st.get_state(ch.id, "seed_next", "0"))

    def seed_done(self, ch: ChannelCfg, now: float):
        left = int(self.st.get_state(ch.id, "seed_left", "0")) - 1
        self.st.set_state(ch.id, "seed_left", str(max(left, 0)))
        self.st.set_state(ch.id, "seed_next", str(now + SEED_GAP_MINUTES * 60))

    def publish_due(self, ch: ChannelCfg, now: float | None = None) -> int | None:
        now = now or time.time()
        if self.paused(ch):
            return None
        if self.seed_step(ch, now):
            post = self.st.next_queued(ch.id)
            if not post:
                return None
            msg_id = self.send(ch, post)
            if msg_id is not None:
                self.seed_done(ch, now)
            return msg_id
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
            partner = self.channels[self.rng.choice([x for x in ch.promo.partners
                                                     if self.channels[x].username])]
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
            mid = self.bot(ch.bot_token).send_post(ch.chat_id, post["text"], post["image"],
                                                   fallback_image=post["fallback_image"])
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

    # --- relay: дословный репост официальных источников ---
    def relay_text(self, ch: ChannelCfg, item) -> str:
        parts = [(item["body"] or html.escape(item["summary"] or item["title"]))[:3700]]
        tail = []
        if ch.relay.attribution and item["source"]:
            label = "Джерело" if ch.style.language == "uk" else "Источник"
            tail.append(f'{label}: <a href="{html.escape(item["url"])}">{html.escape(item["source"])}</a>')
        if ch.style.signature:
            tail.append(html.escape(ch.style.signature))
        if tail:
            parts.append(" · ".join(tail))
        return "\n\n".join(parts)

    def relay(self, ch: ChannelCfg) -> int:
        sources.collect(ch, self.st)
        if not self.st.get_state(ch.id, "primed"):
            # первый запуск: не вываливаем в канал всю историю источника
            for it in self.st.new_in_order(ch.id, limit=1000):
                self.st.set_item_status(it["id"], "rejected", "before start")
            self.st.set_state(ch.id, "primed", "1")
            return 0
        # тревога, которая не ушла за 10 минут, уже неактуальна
        self.st.db.execute("UPDATE posts SET status='rejected', error='stale' WHERE channel=? "
                           "AND status='queued' AND created_ts<?", (ch.id, time.time() - 600))
        self.st.db.commit()
        sent = 0
        now = time.time()
        if self.seed_step(ch, now):
            ids = [x for x in (self.st.get_state(ch.id, "seed_items") or "").split(",") if x]
            if ids:
                self.st.set_item_status(int(ids[0]), "new")
                self.st.set_state(ch.id, "seed_items", ",".join(ids[1:]))
                self.seed_done(ch, now)
            else:
                self.st.set_state(ch.id, "seed_left", "0")
        for it in self.st.new_in_order(ch.id, limit=ch.relay.max_per_poll):
            self.st.set_item_status(it["id"], "used")
            status = "pending_review" if ch.mode == "review" else "queued"
            text = self.relay_text(ch, it)
            image = None
            if ch.style.images == "card":
                try:  # шаблон по типу тревоги, рисуется один раз и дальше берётся с диска
                    image = cards.alert_card(it["title"] + " " + (it["summary"] or ""), ch.style.card_label or ch.title,
                                             self.s.cards_dir)
                except Exception as e:
                    log.warning("[%s] alert card failed: %s", ch.id, e)
            elif ch.style.images != "none":
                image = it["image"]
            pid = self.st.add_post(ch.id, text, status, item_id=it["id"], image=image, kind="relay")
            if status == "pending_review":
                self.send_for_review(ch, pid, text, image)
        for _ in range(ch.relay.max_per_poll):
            post = self.st.next_queued(ch.id)
            if not post or self.paused(ch):
                break
            if self.send(ch, post) is not None:
                sent += 1
            else:
                break
        return sent

    def market_summary_due(self, ch: ChannelCfg, now: float) -> bool:
        m = ch.market_summary
        if not m.enabled:
            return False
        from datetime import datetime
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(now, ZoneInfo(ch.schedule.timezone))
        hh, mm = (int(x) for x in m.time.split(":"))
        today = local.strftime("%Y-%m-%d")
        return (local.hour, local.minute) >= (hh, mm) and self.st.get_state(ch.id, "summary_date") != today

    def market_summary(self, ch: ChannelCfg, now: float) -> int | None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.fromtimestamp(now, ZoneInfo(ch.schedule.timezone)).strftime("%Y-%m-%d")
        with httpx.Client(headers={"User-Agent": sources.UA}, timeout=20) as c:
            coins = market.fetch_markets(c)
            fng = market.fear_greed(c)
        text = market.build_summary(coins, ch.market_summary.coins, ch.market_summary.movers, ch.style.language, fng)
        if ch.style.signature:
            text += "\n\n" + html.escape(ch.style.signature)
        self.st.set_state(ch.id, "summary_date", today)
        image = None
        if ch.style.images != "none":
            try:
                image = cards.market_card(coins, ch.market_summary.coins, ch.style.language,
                                          ch.style.card_label or ch.title, ch.style.card_color,
                                          self.s.cards_dir, ch.schedule.timezone)
            except Exception as e:
                log.warning("[%s] market card failed: %s", ch.id, e)
        status = "queued" if ch.market_summary.skip_review or ch.mode == "auto" else "pending_review"
        pid = self.st.add_post(ch.id, text, status, kind="summary", image=image)
        if status == "pending_review":
            self.send_for_review(ch, pid, text, image)
            return None
        return self.send(ch, self.st.get_post(pid))

    def digest_due(self, ch: ChannelCfg, now: float) -> bool:
        g = ch.digest
        if not g.enabled or not ch.username:
            return False
        local = datetime.fromtimestamp(now, ZoneInfo(ch.schedule.timezone))
        hh, mm = (int(x) for x in g.time.split(":"))
        return (local.hour, local.minute) >= (hh, mm) and self.st.get_state(ch.id, "digest_date") != \
            local.strftime("%Y-%m-%d")

    def digest(self, ch: ChannelCfg, now: float) -> int | None:
        """Вечерний дайджест: заголовки постов дня, каждый ссылкой на сам пост в канале."""
        g = ch.digest
        self.st.set_state(ch.id, "digest_date",
                          datetime.fromtimestamp(now, ZoneInfo(ch.schedule.timezone)).strftime("%Y-%m-%d"))
        rows = self.st.db.execute(
            "SELECT text, message_id FROM posts WHERE channel=? AND kind='news' AND status='published' "
            "AND message_id IS NOT NULL AND published_ts>=? ORDER BY published_ts",
            (ch.id, day_start_ts(now, ch.schedule))).fetchall()
        if len(rows) < 3:
            return None
        if len(rows) > g.max_items:  # равномерно по всему дню, а не только вечер
            step = len(rows) / g.max_items
            rows = [rows[int(i * step)] for i in range(g.max_items)]
        user = ch.username.lstrip("@")
        lines = []
        for r in rows:
            h = headline(r["text"])
            h = h if len(h) <= 120 else h[:117].rsplit(" ", 1)[0] + "…"
            lines.append(f'▪️ <a href="https://t.me/{html.escape(user)}/{r["message_id"]}">{html.escape(h)}</a>')
        date = datetime.fromtimestamp(now, ZoneInfo(ch.schedule.timezone)).strftime("%d.%m")
        text = f"<b>{html.escape(g.title)} · {date}</b>\n\n" + "\n\n".join(lines) + f"\n\n➖➖➖\n{channel_link(user)}"
        image = None
        if ch.style.images != "none":
            try:
                image = cards.title_card(f"{cards.clean(g.title)} · {date}", ch.style.card_label or ch.title,
                                         ch.style.card_color, ch.style.signature, self.s.cards_dir,
                                         ch.schedule.timezone)
            except Exception as e:
                log.warning("[%s] digest card failed: %s", ch.id, e)
        pid = self.st.add_post(ch.id, text, "queued", kind="digest", image=image)
        return self.send(ch, self.st.get_post(pid))

    def tick(self, now: float | None = None):
        now = now or time.time()
        for ch in self.channels.values():
            if not ch.enabled or self.paused(ch):
                continue
            if ch.kind == "relay":
                last = float(self.st.get_state(ch.id, "last_collect", "0"))
                if now - last >= ch.relay.poll_seconds:
                    self.st.set_state(ch.id, "last_collect", str(now))
                    try:
                        self.relay(ch)
                    except Exception as e:
                        log.exception("[%s] relay crashed: %s", ch.id, e)
                continue
            last = float(self.st.get_state(ch.id, "last_collect", "0"))
            # ночью не готовим посты: к утру они устареют, а вызовы Claude уже потрачены
            soon = datetime.fromtimestamp(now + 3600, ZoneInfo(ch.schedule.timezone))
            seeding = int(self.st.get_state(ch.id, "seed_left", "0")) > 0  # разовый выпуск ждёт готовый пост
            if now - last >= self.s.collect_every_minutes * 60 and (seeding or in_active_hours(soon, ch.schedule)):
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
            if ch.enabled and ch.kind == "news":
                try:
                    if self.market_summary_due(ch, now):
                        self.market_summary(ch, now)
                except Exception as e:
                    log.warning("[%s] market summary failed: %s", ch.id, e)
                try:
                    if self.digest_due(ch, now):
                        self.digest(ch, now)
                except Exception as e:
                    log.warning("[%s] digest failed: %s", ch.id, e)
                try:
                    self.publish_due(ch, now)
                except Exception as e:
                    log.exception("[%s] publish crashed: %s", ch.id, e)
        try:
            health.daily(self, now)
        except Exception as e:
            log.warning("health report crashed: %s", e)

    def fix_promo_links(self):
        """Старые посты взаимопиара с некликабельным username: дописываем ссылку (один раз)."""
        rows = self.st.db.execute(
            "SELECT * FROM posts WHERE kind='promo' AND status IN ('published','queued')").fetchall()
        for p in rows:
            ch = self.channels.get(p["channel"])
            m = re.search(r"👉 @?([A-Za-z0-9_]{4,})\s*$", p["text"])
            if not ch or not m:
                continue
            text = p["text"][:m.start()] + "👉 " + channel_link(m.group(1))
            self.st.db.execute("UPDATE posts SET text=? WHERE id=?", (text, p["id"]))
            self.st.db.commit()
            if p["status"] == "published" and p["message_id"] and ch.bot_token and ch.chat_id and not self.s.dry_run:
                try:
                    self.bot(ch.bot_token).call("editMessageText", chat_id=ch.chat_id, message_id=p["message_id"],
                                                text=text, parse_mode="HTML",
                                                link_preview_options={"is_disabled": True})
                    log.info("[%s] promo post %d link fixed", ch.id, p["id"])
                except Exception as e:
                    log.warning("[%s] promo post %d not edited: %s", ch.id, p["id"], e)

    def sync_profiles(self):
        """Название, описание и аватар канала из конфига; каждое меняется только когда отличается от прошлого."""
        for ch in self.channels.values():
            pr = ch.profile
            if not ch.enabled or not ch.bot_token or not ch.chat_id or self.s.dry_run:
                continue
            bot = self.bot(ch.bot_token)
            photo_sig = ""
            if pr.photo and os.path.exists(pr.photo):
                photo_sig = f"{pr.photo}:{os.path.getsize(pr.photo)}"
            for key, value, call in (
                ("title", pr.title, lambda v: bot.call("setChatTitle", chat_id=ch.chat_id, title=v)),
                ("description", pr.description,
                 lambda v: bot.call("setChatDescription", chat_id=ch.chat_id, description=v)),
                ("photo", photo_sig, lambda v: bot.set_chat_photo(ch.chat_id, pr.photo)),
            ):
                if not value or self.st.get_state(ch.id, f"profile_{key}") == value:
                    continue
                try:
                    call(value)
                    self.st.set_state(ch.id, f"profile_{key}", value)
                    log.info("[%s] channel %s updated", ch.id, key)
                except Exception as e:
                    log.warning("[%s] channel %s not updated: %s", ch.id, key, e)

    def run_forever(self):
        health.install()
        self.fix_promo_links()
        self.sync_profiles()
        if self.s.seed_once and self.st.get_state("_seed", "done") != self.s.seed_once:
            for ch in self.channels.values():
                if ch.enabled and (not self.s.seed_channels or ch.id in self.s.seed_channels):
                    self.seed(ch, self.s.seed_posts)
            self.st.set_state("_seed", "done", self.s.seed_once)
            log.info("seed %s: %d posts per channel", self.s.seed_once, self.s.seed_posts)
        log.info("engine started: %d channels, dry_run=%s", len(self.channels), self.s.dry_run)
        polls = [c.relay.poll_seconds for c in self.channels.values() if c.enabled and c.kind == "relay"]
        sleep = min([self.s.tick_seconds, *polls])
        while True:
            self.tick()
            time.sleep(sleep)
