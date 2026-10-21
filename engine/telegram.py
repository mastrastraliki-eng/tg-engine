"""Тонкий клиент Telegram Bot API."""
from __future__ import annotations

import json
import logging
import os
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

    def set_chat_photo(self, chat_id, path: str):
        with open(path, "rb") as f:
            r = self.http.post(self.base + "setChatPhoto", data={"chat_id": chat_id}, files={"photo": f})
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", r.status_code), data.get("description", ""))
        return data["result"]

    def get_me(self):
        return self.call("getMe")

    def _photo(self, chat_id, photo: str, caption: str | None, reply_markup):
        """photo: ссылка или путь к локальному файлу (свою карточку загружаем)."""
        params = {k: v for k, v in dict(chat_id=chat_id, caption=caption, parse_mode="HTML" if caption else None,
                                         reply_markup=reply_markup).items() if v is not None}
        if os.path.isfile(photo):
            if "reply_markup" in params:
                params["reply_markup"] = json.dumps(params["reply_markup"])
            with open(photo, "rb") as f:
                for attempt in range(3):
                    f.seek(0)
                    r = self.http.post(self.base + "sendPhoto", data=params, files={"photo": f})
                    data = r.json()
                    if data.get("ok"):
                        return data["result"]
                    retry = data.get("parameters", {}).get("retry_after")
                    if data.get("error_code") == 429 and retry and attempt < 2:
                        time.sleep(int(retry) + 1)
                        continue
                    raise TelegramError(data.get("error_code", r.status_code), data.get("description", ""))
        if photo.startswith("video:"):  # видео из поста источника: Telegram скачает его сам
            return self.call("sendVideo", video=photo[6:], supports_streaming=True, **params)
        return self.call("sendPhoto", photo=photo, **params)

    def send_post(self, chat_id: str, text: str, image: str | None = None, reply_markup=None,
                  fallback_image: str | None = None) -> int:
        """Пост с картинкой. Если чужое фото не загрузилось, пробуем свою карточку, потом текст."""
        plain_len = len(re.sub(r"<[^>]+>", "", text))
        for img in [i for i in (image, fallback_image) if i]:
            try:
                if plain_len <= 1024:
                    return self._photo(chat_id, img, text, reply_markup)["message_id"]
                # подпись к фото не длиннее 1024 символов: картинка отдельно, текст следом
                self._photo(chat_id, img, None, None)
                break
            except TelegramError as e:
                if e.fatal:
                    raise
                log.info("photo failed (%s)", e)
        msg = self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                        link_preview_options={"is_disabled": True}, reply_markup=reply_markup)
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
