"""Ежедневная сводка крипторынка. Цифры берутся прямо из CoinGecko, без LLM."""
from __future__ import annotations

import html

import httpx

API = "https://api.coingecko.com/api/v3/coins/markets"


def fetch_markets(client: httpx.Client, per_page: int = 100) -> list[dict]:
    r = client.get(API, params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": per_page,
                                "page": 1, "price_change_percentage": "24h"})
    r.raise_for_status()
    return r.json()


def _price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.0f}".replace(",", " ")
    if p >= 1:
        return f"${p:,.2f}".replace(",", " ")
    return f"${p:.4g}"


def _chg(c: float | None) -> str:
    if c is None:
        return "—"
    arrow = "🟢" if c >= 0 else "🔴"
    return f"{arrow} {c:+.1f}%"


def build_summary(coins: list[dict], featured: list[str], movers: int = 3, lang: str = "uk") -> str:
    by_id = {c["id"]: c for c in coins}
    t = {"uk": ("Ринок крипти за добу", "Найбільше зросли", "Найбільше впали", "Дані: CoinGecko, ціни в USD."),
         "ru": ("Рынок крипты за сутки", "Больше всего выросли", "Больше всего упали", "Данные: CoinGecko, цены в USD.")}[
        "ru" if lang == "ru" else "uk"]
    lines = [f"<b>📊 {t[0]}</b>", ""]
    for cid in featured:
        c = by_id.get(cid)
        if c:
            lines.append(f"<b>{html.escape(c['symbol'].upper())}</b> {_price(c['current_price'])}  "
                         f"{_chg(c.get('price_change_percentage_24h'))}")
    ranked = [c for c in coins if c.get("price_change_percentage_24h") is not None and c["id"] not in featured
              and "usd" not in c["symbol"].lower()]  # стейблкоины не интересны
    ranked.sort(key=lambda c: c["price_change_percentage_24h"], reverse=True)
    if ranked and movers:
        lines += ["", f"<b>{t[1]}</b>"]
        lines += [f"{html.escape(c['symbol'].upper())} {_chg(c['price_change_percentage_24h'])}" for c in ranked[:movers]]
        lines += ["", f"<b>{t[2]}</b>"]
        lines += [f"{html.escape(c['symbol'].upper())} {_chg(c['price_change_percentage_24h'])}" for c in ranked[-movers:][::-1]]
    lines += ["", f"<i>{t[3]}</i>"]
    return "\n".join(lines)
