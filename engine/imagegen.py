"""ИИ-иллюстрации. Только для креативных каналов (крипта), не для новостей о войне.

Несколько разных сервисов по очереди: если у одного кончилась бесплатная квота или он лежит,
пробуем следующий. Не получилось ни у кого — пост уйдёт с фото источника или карточкой.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import urllib.parse
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
STYLE = ("Editorial illustration for a crypto news Telegram channel, 16:9, modern, clean, bold colors. "
         "No text, no letters, no logos of real companies, no real people's faces. Subject: ")


def _cloudflare(client: httpx.Client, prompt: str, model: str) -> bytes | None:
    """Cloudflare Workers AI, FLUX schnell. Бесплатная дневная квота, карта не нужна."""
    acct, token = os.environ.get("CLOUDFLARE_ACCOUNT_ID"), os.environ.get("CLOUDFLARE_API_TOKEN")
    if not acct or not token:
        return None
    r = client.post(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                    headers={"Authorization": f"Bearer {token}"}, json={"prompt": prompt, "steps": 6})
    if r.status_code != 200:
        log.info("cloudflare image: %s %s", r.status_code, r.text[:200])
        return None
    img = r.json().get("result", {}).get("image")
    return base64.b64decode(img) if img else None


def _pollinations(client: httpx.Client, prompt: str, model: str) -> bytes | None:
    """Pollinations: работает без ключа (медленнее, с лимитом); с бесплатным токеном без водяного знака."""
    headers = {}
    if os.environ.get("POLLINATIONS_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['POLLINATIONS_TOKEN']}"
    r = client.get("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt),
                   params={"width": 1280, "height": 720, "model": "flux", "nologo": "true", "private": "true"},
                   headers=headers, follow_redirects=True)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        log.info("pollinations image: %s %s", r.status_code, r.text[:200] if "text" in r.headers.get("content-type", "") else "")
        return None
    return r.content


def _gemini(client: httpx.Client, prompt: str, model: str) -> bytes | None:
    """Google Gemini. Для картинок нужен проект с подключённой оплатой."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    r = client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}}})
    if r.status_code != 200:
        log.info("gemini image: %s %s", r.status_code, r.text[:200])
        return None
    for cand in r.json().get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
            if data:
                return base64.b64decode(data)
    return None


PROVIDERS = {"cloudflare": _cloudflare, "pollinations": _pollinations, "gemini": _gemini}


def generate(prompt: str, model: str, out_dir: str | Path, client: httpx.Client | None = None,
             providers: list[str] = ("cloudflare", "pollinations", "gemini"), style: str = "") -> str | None:
    if not prompt.strip():
        return None
    full = (style.strip() + " No text, no letters, no real company logos, no real people's faces. Scene: " + prompt
            if style.strip() else STYLE + prompt)
    own = client is None
    client = client or httpx.Client(timeout=120)
    try:
        for name in providers:
            try:
                data = PROVIDERS[name](client, full, model)
            except Exception as e:
                log.info("%s image error: %s", name, e)
                data = None
            if data:
                out = Path(out_dir)
                out.mkdir(parents=True, exist_ok=True)
                ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
                p = out / f"ai-{hashlib.sha1(prompt.encode()).hexdigest()[:16]}{ext}"
                p.write_bytes(data)
                log.info("AI image from %s", name)
                return str(p)
        log.warning("no image service produced a picture")
        return None
    finally:
        if own:
            client.close()
