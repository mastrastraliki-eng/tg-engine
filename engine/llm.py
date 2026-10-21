"""Бесплатные LLM по очереди: Groq → Gemini → Cloudflare → OpenRouter (→ Anthropic, если включён).

У каждого провайдера может быть несколько ключей (через запятую). Ключ, упёршийся в лимит (429)
или отозванный (401/403), откладывается до следующих суток, и берётся следующий.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

# name: (url, env ключей, модели по очереди, поддерживает response_format=json_object)
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY",
             ["moonshotai/kimi-k2-instruct-0905", "llama-3.3-70b-versatile"], True),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY",
               ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"], True),
    "cloudflare": ("https://api.cloudflare.com/client/v4/accounts/{acct}/ai/v1/chat/completions",
                   "CLOUDFLARE_API_TOKEN", ["@cf/meta/llama-3.3-70b-instruct-fp8-fast"], False),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY",
                   ["meta-llama/llama-3.3-70b-instruct:free"], False),
}
COOLDOWN = 6 * 3600  # ключ отозван или суточный лимит: пробуем снова через 6 часов


def cooldown(r: httpx.Response) -> float:
    """Минутный лимит ждём сколько скажут, суточный или отозванный ключ откладываем на часы."""
    if r.status_code != 429:
        return COOLDOWN
    try:
        wait = float(r.headers.get("retry-after", ""))
    except ValueError:
        wait = 0
    if 0 < wait < 3600:
        return max(wait, 60)
    return 1800 if not wait else COOLDOWN


def keys(env: str) -> list[str]:
    raw = ",".join(filter(None, [os.environ.get(env, ""), os.environ.get(env + "S", "")]))
    return list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))


def parse_json(text: str, schema: type[BaseModel]) -> BaseModel | None:
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    m = re.search(r"(?s)\{.*\}", text)
    if not m:
        return None
    try:
        return schema.model_validate(json.loads(m.group(0)))
    except (json.JSONDecodeError, ValidationError):
        return None


def json_instruction(schema: type[BaseModel]) -> str:
    fields = "\n".join(f'- "{n}": {f.description or n}' for n, f in schema.model_fields.items())
    return ("\n\nОтветь строго одним JSON-объектом без пояснений и без ```, с полями:\n" + fields
            + "\nВ строках JSON переносы строк пиши как \\n.")


class FreeLLM:
    def __init__(self, storage, order: list[str], models: dict[str, list[str]] | None = None,
                 client: httpx.Client | None = None):
        self.st = storage
        self.order = order
        self.models = models or {}
        self.http = client or httpx.Client(timeout=90)

    def _cooling(self, slot: str) -> bool:
        until = self.st.get_state("_llm", slot)
        return bool(until) and float(until) > time.time()

    def _cool(self, slot: str, why: str, seconds: float = COOLDOWN):
        log.warning("LLM %s off for %d min: %s", slot, seconds // 60, why)
        self.st.set_state("_llm", slot, str(time.time() + seconds))

    def available(self) -> list[str]:
        out = []
        for name in self.order:
            if name in PROVIDERS:
                out += [f"{name}#{i}" for i, _ in enumerate(keys(PROVIDERS[name][1]))]
        return out

    def complete(self, system: str, user: str, schema: type[BaseModel]) -> tuple[BaseModel | None, str]:
        """Возвращает (результат, кто ответил). (None, "") — ни один провайдер не смог."""
        system = system + json_instruction(schema)
        for name in self.order:
            if name not in PROVIDERS:
                continue
            url, env, default_models, json_mode = PROVIDERS[name]
            if "{acct}" in url:
                acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
                if not acct:
                    continue
                url = url.format(acct=acct)
            for i, key in enumerate(keys(env)):
                slot = f"{name}#{i}"
                if self._cooling(slot):
                    continue
                for model in self.models.get(name) or default_models:
                    body = {"model": model, "temperature": 0.7, "max_tokens": 2500,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
                    if json_mode:
                        body["response_format"] = {"type": "json_object"}
                    try:
                        r = self.http.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
                    except httpx.HTTPError as e:
                        log.info("LLM %s %s: %s", slot, model, type(e).__name__)
                        break
                    if r.status_code in (401, 402, 403, 429):
                        self._cool(slot, f"{r.status_code} {r.text[:120]}", cooldown(r))
                        break
                    if r.status_code != 200:
                        log.info("LLM %s %s: %s %s", slot, model, r.status_code, r.text[:200])
                        continue  # модель недоступна: следующая модель того же ключа
                    try:
                        text = r.json()["choices"][0]["message"]["content"]
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    res = parse_json(text, schema)
                    if res is not None:
                        return res, f"{name}:{model}"
                    log.info("LLM %s %s: bad JSON", slot, model)
        return None, ""
