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


def build_summary(coins: list[dict], featured: list[str], movers: int = 3, lang: str = "uk",
                  fng: tuple[int, str] | None = None) -> str:
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
    if fng:
        lines += ["", fear_greed_line(fng, lang)]
    lines += ["", f"<i>{t[3]}</i>"]
    return "\n".join(lines)


# монеты, которые узнаём в тексте поста: (шаблон, id в CoinGecko, тикер)
COINS = [
    (r"биткоин|биткойн|біткоїн|bitcoin|\bbtc\b", "bitcoin", "BTC"),
    (r"эфириум|ефіріум|ethereum|\beth\b|\bэфир", "ethereum", "ETH"),
    (r"солан|solana|\bsol\b", "solana", "SOL"),
    (r"\bxrp\b|ripple|рипл", "ripple", "XRP"),
    (r"\bton\b|toncoin|тонкоин", "the-open-network", "TON"),
    (r"\bbnb\b|binance coin", "binancecoin", "BNB"),
    (r"dogecoin|\bdoge\b|догикоин|додж", "dogecoin", "DOGE"),
    (r"cardano|\bada\b|кардано", "cardano", "ADA"),
    (r"\btron\b|\btrx\b|трон", "tron", "TRX"),
    (r"chainlink|\blink\b", "chainlink", "LINK"),
    (r"avalanche|\bavax\b", "avalanche-2", "AVAX"),
    (r"\bsui\b", "sui", "SUI"),
    (r"hyperliquid|\bhype\b", "hyperliquid", "HYPE"),
    (r"litecoin|\bltc\b|лайткоин", "litecoin", "LTC"),
    (r"polkadot|\bdot\b", "polkadot", "DOT"),
    (r"pepe", "pepe", "PEPE"),
    (r"shiba|\bshib\b", "shiba-inu", "SHIB"),
]


def detect_coin(text: str) -> tuple[str, str] | None:
    """Главная монета поста (первая упомянутая): (id CoinGecko, тикер) или None."""
    import re
    best = None
    low = re.sub(r"<[^>]+>", " ", text or "").lower()
    for pattern, cid, sym in COINS:
        m = re.search(pattern, low)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), cid, sym)
    return (best[1], best[2]) if best else None


def fetch_ohlc(client: httpx.Client, coin_id: str, days: int = 1) -> list[list[float]]:
    """Свечи [ts, open, high, low, close] из CoinGecko (за сутки — по 30 минут)."""
    r = client.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                   params={"vs_currency": "usd", "days": days})
    r.raise_for_status()
    return [c for c in r.json() if len(c) == 5]


def fear_greed(client: httpx.Client) -> tuple[int, str] | None:
    """Индекс страха и жадности (alternative.me): значение 0–100 и его название."""
    try:
        r = client.get("https://api.alternative.me/fng/", params={"limit": 1})
        r.raise_for_status()
        d = r.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except Exception:
        return None


FNG_NAMES = {"Extreme Fear": ("Сильный страх", "Сильний страх", "😱"), "Fear": ("Страх", "Страх", "😨"),
             "Neutral": ("Нейтрально", "Нейтрально", "😐"), "Greed": ("Жадность", "Жадібність", "🤑"),
             "Extreme Greed": ("Сильная жадность", "Сильна жадібність", "🚀")}


def fear_greed_line(fng: tuple[int, str] | None, lang: str = "ru") -> str:
    if not fng:
        return ""
    ru, uk, emoji = FNG_NAMES.get(fng[1], (fng[1], fng[1], "🧭"))
    head = "Индекс страха и жадности" if lang == "ru" else "Індекс страху і жадібності"
    return f"{emoji} {head}: <b>{fng[0]}</b> — {ru if lang == 'ru' else uk}"
