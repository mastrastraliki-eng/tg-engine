"""Тонкий клиент Telegram Bot API."""
from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description

    @property
    def fatal(self) -> bool:
        """Ошибки, после которых канал надо ставить на паузу: бот выкинут, токен отозван, чат не найден."""
        return self.code in (401, 403) or (self.code == 400 and "chat not found" in self.description.lower())


class Bot:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.http = client or httpx.Client(timeout=40)

    def call(self, method: str, **params):
        for attempt in range(3):
            r = self.http.post(self.base + method, json={k: v for k, v in params.items() if v is not None})
            data = r.json()
            if data.get("ok"):
                return data["result"]
            retry = data.get("parameters", {}).get("retry_after")
            if data.get("error_code") == 429 and retry and attempt < 2:
                log.warning("flood wait %ss on %s", retry, method)
                time.sleep(int(retry) + 1)
                continue
            raise TelegramError(data.get("error_code", r.status_code), data.get("description", ""))

    def get_me(self):
        return self.call("getMe")

    def send_post(self, chat_id: str, text: str, image: str | None = None, reply_markup=None) -> int:
        plain_len = len(re.sub(r"<[^>]+>", "", text))
        if image and plain_len <= 1024:
            try:
                msg = self.call("sendPhoto", chat_id=chat_id, photo=image, caption=text,
                                parse_mode="HTML", reply_markup=reply_markup)
                return msg["message_id"]
            except TelegramError as e:
                if e.fatal:
                    raise
                log.info("photo failed (%s), sending text", e)
        msg = self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                        link_preview_options={"is_disabled": not bool(image), "url": image} if image
                        else {"is_disabled": True},
                        reply_markup=reply_markup)
        return msg["message_id"]

    def get_updates(self, offset: int | None):
        return self.call("getUpdates", offset=offset, timeout=0, allowed_updates=["callback_query"])

    def answer_callback(self, cb_id: str, text: str):
        try:
            self.call("answerCallbackQuery", callback_query_id=cb_id, text=text)
        except TelegramError:
            pass

    def edit_markup(self, chat_id, message_id):
        try:
            self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                      reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass
