"""Картинки для постов: фирменная карточка с заголовком, карточка сводки рынка, шаблоны тревог.

Рисуем сами (Pillow), поэтому прав на изображение ни у кого, кроме нас, нет.
"""
from __future__ import annotations

import hashlib
import re
import textwrap
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
FONT_BOLD = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
FONT_REG = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"]
# всё, что шрифт не нарисует (эмодзи, флаги, стрелки-пиктограммы)
NON_TEXT = re.compile(r"[\U0001F000-\U0001FFFF\u2600-\u27BF\uFE0F\u200d]")

ALERT_KINDS = [  # (ключ, шаблон в тексте, подпись, цвет)
    ("clear", r"відбій", "ВІДБІЙ ТРИВОГИ", "#1f9d55"),
    ("alert", r"тривог", "ПОВІТРЯНА ТРИВОГА", "#d32f2f"),
    ("threat", r"бпла|шахед|ракет|балістик|загроз|авіабомб|каб", "ЗАГРОЗА", "#ef6c00"),
    ("report", r"втрат|оперативн|зведенн", "ЗВЕДЕННЯ", "#1565c0"),
]


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for f in FONT_BOLD if bold else FONT_REG:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _background(accent: str) -> Image.Image:
    """Тёмный вертикальный градиент с оттенком цвета канала и полосой акцента слева."""
    a = _rgb(accent)
    top, bottom = tuple(int(c * 0.35) for c in a), (12, 14, 20)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    d.rectangle([0, 0, 18, H], fill=a)
    return img


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", NON_TEXT.sub("", text)).strip()


def _fit_title(d: ImageDraw.ImageDraw, title: str, box_w: int, max_lines: int = 5):
    for size in (78, 70, 62, 56, 50, 44):
        font = _font(size)
        avg = d.textlength("абвгдежзик", font=font) / 10
        lines = textwrap.wrap(title, width=max(10, int(box_w / avg)))
        if len(lines) <= max_lines and all(d.textlength(l, font=font) <= box_w for l in lines):
            return font, lines, size
    font = _font(44)
    lines = textwrap.wrap(title, width=int(box_w / (d.textlength("абвгдежзик", font=font) / 10)))
    lines = lines[:max_lines]
    lines[-1] = lines[-1].rstrip(" .,") + "…"
    return font, lines, 44


def _save(img: Image.Image, out_dir: str | Path, key: str) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.png"
    img.save(p, optimize=True)
    return str(p)


def _label(d: ImageDraw.ImageDraw, text: str, accent: str, x: int = 80, y: int = 70) -> int:
    font = _font(30)
    w = d.textlength(text, font=font)
    d.rounded_rectangle([x, y, x + w + 44, y + 56], radius=28, fill=_rgb(accent))
    d.text((x + 22, y + 10), text, font=font, fill="white")
    return y + 56


def title_card(title: str, label: str, accent: str = "#2962ff", footer: str = "",
               out_dir: str | Path = "data/cards", tz: str = "Europe/Kyiv") -> str:
    """Карточка поста: метка канала, крупный заголовок, дата."""
    img = _background(accent)
    d = ImageDraw.Draw(img)
    _label(d, clean(label).upper(), accent)
    font, lines, size = _fit_title(d, clean(title), W - 160)
    line_h = int(size * 1.22)
    y = 160 + max(0, (H - 160 - 120 - line_h * len(lines)) // 2)
    for l in lines:
        d.text((80, y), l, font=font, fill="white")
        y += line_h
    date = datetime.fromtimestamp(time.time(), ZoneInfo(tz)).strftime("%d.%m.%Y")
    small = _font(28, bold=False)
    d.text((80, H - 80), date, font=small, fill=(170, 176, 190))
    if footer:
        d.text((W - 80 - d.textlength(footer, font=small), H - 80), footer, font=small, fill=(170, 176, 190))
    return _save(img, out_dir, f"title|{label}|{title}|{accent}|{footer}|{date}")


def alert_kind(text: str) -> tuple[str, str, str]:
    low = clean(text).lower()
    for key, rx, caption, color in ALERT_KINDS:
        if re.search(rx, low):
            return key, caption, color
    return "update", "ОНОВЛЕННЯ", "#455a64"


def alert_card(text: str, label: str = "", out_dir: str | Path = "data/cards") -> str:
    """Шаблон тревоги по типу. Рисуется один раз на тип и потом берётся из файла, поэтому не тормозит."""
    key, caption, color = alert_kind(text)
    out = Path(out_dir)
    path = out / f"alert-{key}.png"
    if path.exists():
        return str(path)
    img = Image.new("RGB", (W, H), _rgb(color))
    d = ImageDraw.Draw(img)
    for y in range(H):  # лёгкое затемнение книзу
        k = 1 - 0.35 * y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c * k) for c in _rgb(color)))
    font = _font(104)
    w = d.textlength(caption, font=font)
    if w > W - 120:
        font = _font(int(104 * (W - 120) / w))
        w = d.textlength(caption, font=font)
    d.text(((W - w) / 2, H / 2 - 80), caption, font=font, fill="white")
    if label:
        small = _font(36, bold=False)
        lw = d.textlength(clean(label), font=small)
        d.text(((W - lw) / 2, H / 2 + 60), clean(label), font=small, fill=(255, 255, 255))
    out.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)
    return str(path)


def market_card(coins: list[dict], featured: list[str], lang: str = "ru", label: str = "",
                accent: str = "#f7931a", out_dir: str | Path = "data/cards", tz: str = "Europe/Kyiv") -> str:
    """Карточка сводки рынка: тикер, цена и изменение за 24 часа."""
    from .market import _price
    by_id = {c["id"]: c for c in coins}
    rows = [by_id[c] for c in featured if c in by_id]
    img = _background(accent)
    d = ImageDraw.Draw(img)
    head = "Рынок крипты за сутки" if lang == "ru" else "Ринок крипти за добу"
    y0 = _label(d, clean(label).upper(), accent) if label else 70
    d.text((80, y0 + 20), head, font=_font(56), fill="white")
    cols = 2 if len(rows) > 4 else 1
    per_col = (len(rows) + cols - 1) // cols
    col_w = (W - 160) // cols
    top, row_h = y0 + 120, min(110, (H - y0 - 200) // max(1, per_col))
    sym_f, price_f, chg_f = _font(46), _font(40, bold=False), _font(40)
    for i, c in enumerate(rows):
        x = 80 + (i // per_col) * col_w
        y = top + (i % per_col) * row_h
        ch = c.get("price_change_percentage_24h")
        d.text((x, y), c["symbol"].upper(), font=sym_f, fill="white")
        px = x + max(150, int(d.textlength(c["symbol"].upper(), font=sym_f)) + 24)
        d.text((px, y + 4), _price(c["current_price"]), font=price_f, fill=(215, 220, 230))
        if ch is not None:
            s = f"{ch:+.1f}%"
            color = (76, 217, 100) if ch >= 0 else (255, 90, 90)
            d.text((x + col_w - 40 - d.textlength(s, font=chg_f), y + 4), s, font=chg_f, fill=color)
    small = _font(26, bold=False)
    date = datetime.fromtimestamp(time.time(), ZoneInfo(tz)).strftime("%d.%m.%Y")
    d.text((80, H - 70), f"{date} · CoinGecko, USD", font=small, fill=(170, 176, 190))
    return _save(img, out_dir, f"market|{date}|{time.time()}")


def chart_card(symbol: str, ohlc: list[list[float]], label: str = "", accent: str = "#f7931a",
               out_dir: str | Path = "data/cards", tz: str = "Europe/Kyiv", period: str = "24H") -> str:
    """Свечной график монеты в стиле биржевого терминала: пара, цена, изменение, свечи, шкала цен."""
    from .market import _price
    bg, grid, muted = (13, 17, 23), (33, 38, 48), (140, 148, 160)
    up, down = (38, 196, 133), (240, 73, 90)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 6], fill=_rgb(accent))
    first, last = ohlc[0][1], ohlc[-1][4]
    chg = (last / first - 1) * 100 if first else 0.0
    color = up if chg >= 0 else down
    # шапка: пара, цена, изменение, метка канала
    d.text((60, 40), f"{symbol}/USD", font=_font(44), fill="white")
    pw = d.textlength(f"{symbol}/USD", font=_font(44))
    d.text((60 + pw + 28, 46), _price(last), font=_font(40, bold=False), fill=color)
    tag = f"{chg:+.2f}%"
    tf = _font(30)
    tx = 60 + pw + 28 + d.textlength(_price(last), font=_font(40, bold=False)) + 24
    tw = d.textlength(tag, font=tf)
    d.rounded_rectangle([tx, 48, tx + tw + 28, 96], radius=10, fill=color)
    d.text((tx + 14, 54), tag, font=tf, fill="white")
    if label:
        lf = _font(26, bold=False)
        lw = d.textlength(clean(label), font=lf)
        d.text((W - 60 - lw, 56), clean(label), font=lf, fill=muted)
    # поле графика
    left, top, right, bottom = 60, 130, W - 170, H - 80
    hi = max(c[2] for c in ohlc)
    lo = min(c[3] for c in ohlc)
    span = (hi - lo) or hi * 0.01 or 1
    hi, lo = hi + span * 0.06, lo - span * 0.06

    def y(v):
        return bottom - (v - lo) / (hi - lo) * (bottom - top)

    sf = _font(22, bold=False)
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        yy = y(v)
        d.line([(left, yy), (right, yy)], fill=grid, width=1)
        d.text((right + 14, yy - 12), _price(v), font=sf, fill=muted)
    n = len(ohlc)
    step = (right - left) / n
    body = max(3, int(step * 0.62))
    for i, (_, o, h, l, c) in enumerate(ohlc):
        cx = left + step * (i + 0.5)
        col = up if c >= o else down
        d.line([(cx, y(h)), (cx, y(l))], fill=col, width=2)
        y1, y2 = sorted((y(o), y(c)))
        d.rectangle([cx - body / 2, y1, cx + body / 2, max(y2, y1 + 2)], fill=col)
    # линия последней цены
    ly = y(last)
    for x in range(left, right, 14):
        d.line([(x, ly), (x + 7, ly)], fill=color, width=2)
    d.rounded_rectangle([right + 6, ly - 17, W - 20, ly + 17], radius=6, fill=color)
    d.text((right + 14, ly - 13), _price(last), font=sf, fill="white")
    # подписи времени
    zone = ZoneInfo(tz)
    for i in (0, n // 2, n - 1):
        t = datetime.fromtimestamp(ohlc[i][0] / 1000, zone).strftime("%H:%M")
        cx = left + step * (i + 0.5)
        d.text((cx - d.textlength(t, font=sf) / 2, bottom + 14), t, font=sf, fill=muted)
    date = datetime.fromtimestamp(time.time(), zone).strftime("%d.%m.%Y %H:%M")
    d.text((60, H - 36), f"{period} · {date} · CoinGecko", font=_font(20, bold=False), fill=muted)
    return _save(img, out_dir, f"chart|{symbol}|{ohlc[-1][0]}|{label}|{time.time()}")
