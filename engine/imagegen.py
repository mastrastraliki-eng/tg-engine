"""ИИ-иллюстрации через Google Gemini. Только для креативных каналов (крипта), не для новостей о войне."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
STYLE = ("Editorial illustration for a crypto news Telegram channel, 16:9, modern, clean, bold colors. "
         "No text, no letters, no logos of real companies, no real people's faces. Subject: ")


def generate(prompt: str, model: str, out_dir: str | Path, client: httpx.Client | None = None) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not prompt.strip():
        return None
    own = client is None
    client = client or httpx.Client(timeout=90)
    try:
        r = client.post(API.format(model=model), headers={"x-goog-api-key": key},
                        json={"contents": [{"parts": [{"text": STYLE + prompt}]}],
                              "generationConfig": {"responseModalities": ["IMAGE"],
                                                   "imageConfig": {"aspectRatio": "16:9"}}})
        if r.status_code != 200:
            log.warning("gemini image failed: %s %s", r.status_code, r.text[:200])
            return None
        for cand in r.json().get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
                if data:
                    out = Path(out_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    p = out / f"ai-{hashlib.sha1(prompt.encode()).hexdigest()[:16]}.png"
                    p.write_bytes(base64.b64decode(data))
                    return str(p)
        log.warning("gemini returned no image")
        return None
    except Exception as e:
        log.warning("gemini image error: %s", e)
        return None
    finally:
        if own:
            client.close()
