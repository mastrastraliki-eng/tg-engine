"""Сбор материалов из источников и дедупликация."""
from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from .config import ChannelCfg, SourceCfg

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (compatible; tg-engine/1.0)"
TRACKING = re.compile(r"^(utm_|fbclid|gclid|yclid|ref$|ref_src)")


def normalize_url(url: str) -> str:
    s = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(s.query) if not TRACKING.match(k)]
    host = s.netloc.lower().removeprefix("www.")
    return urlunsplit(("https", host, s.path.rstrip("/"), urlencode(q), ""))


def strip_tracking(url: str) -> str:
    """Убирает utm-метки и прочие трекеры из ссылки, остальное не трогает."""
    s = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(s.query) if not TRACKING.match(k)]
    return urlunsplit((s.scheme, s.netloc, s.path, urlencode(q), s.fragment))


def normalize_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return " ".join(w for w in t.split() if len(w) > 2)


def similarity(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _first_image(entry) -> str | None:
    for m in entry.get("media_content", []) or entry.get("media_thumbnail", []):
        if m.get("url"):
            return m["url"]
    for l in entry.get("links", []):
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href")
    m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "") or "")
    return m.group(1) if m else None


def parse_rss(content: bytes | str, src: SourceCfg) -> list[dict]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        if not e.get("link") or not e.get("title"):
            continue
        ts = None
        for f in ("published_parsed", "updated_parsed"):
            if e.get(f):
                ts = calendar.timegm(e[f])
                break
        out.append(dict(
            url=e["link"], title=strip_html(e["title"]),
            summary=strip_html(e.get("summary", ""))[:2000],
            image=_first_image(e) if src.use_images else None, published_ts=ts,
        ))
    return out


def parse_reddit(data: dict, src: SourceCfg) -> list[dict]:
    out = []
    for ch in data.get("data", {}).get("children", []):
        p = ch.get("data", {})
        if p.get("stickied") or p.get("over_18"):
            continue
        img = None
        if src.use_images and p.get("post_hint") == "image":
            img = p.get("url")
        out.append(dict(
            url="https://www.reddit.com" + p.get("permalink", ""), title=p.get("title", ""),
            summary=(p.get("selftext") or "")[:2000], image=img,
            published_ts=p.get("created_utc"), score=p.get("score", 0),
        ))
    return out


def tg_html_to_text(fragment: str) -> str:
    """HTML поста из веб-превью t.me -> разметка, которую понимает Bot API."""
    from .rewriter import sanitize_html
    t = re.sub(r'<i class="emoji"[^>]*>(?:<b>)?(.*?)(?:</b>)?</i>', r"\1", fragment)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r'(?is)<a\s+href="(https?://[^"]+)"[^>]*>', r'<a href="\1">', t)
    t = re.sub(r"(?is)</?(span|div|tg-emoji|mark)[^>]*>", "", t)
    return sanitize_html(html.unescape(t))


def parse_telegram(page: str, src: SourceCfg) -> list[dict]:
    """Посты публичного канала со страницы https://t.me/s/<channel>."""
    out = []
    blocks = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for b in blocks:
        post = re.search(r'data-post="([^"]+)"', b)
        if not post:
            continue
        # только сам текст поста: у ответов перед ним есть цитата js-message_reply_text
        body = re.search(r'(?s)<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>\s*'
                         r'(?:<div class="tgme_widget_message_footer|<div class="tgme_widget_message_link_preview'
                         r'|<a class="tgme_widget_message_link_preview|<div class="tgme_widget_message_reply_markup)', b)
        if not body:
            continue
        rich = tg_html_to_text(body.group(1))
        plain = strip_html(rich.replace("\n", " \n "))
        ts = None
        dt = re.search(r'<time[^>]+datetime="([^"]+)"', b)
        if dt:
            from datetime import datetime
            try:
                ts = datetime.fromisoformat(dt.group(1)).timestamp()
            except ValueError:
                pass
        img = None
        if src.use_images:
            m = re.search(r"tgme_widget_message_photo_wrap[^>]+background-image:url\('([^']+)'\)", b)
            img = m.group(1) if m else None
        first = next((l.strip() for l in strip_html(rich).split("\n") if l.strip()), plain[:120])
        out.append(dict(url=f"https://t.me/{post.group(1)}", title=first[:200] or plain[:200],
                        summary=plain[:3000], body=rich, image=img, published_ts=ts))
    return out


def fetch_source(client: httpx.Client, src: SourceCfg) -> list[dict]:
    if src.type == "telegram":
        r = client.get(f"https://t.me/s/{src.channel}")
        r.raise_for_status()
        items = parse_telegram(r.text, src)
    elif src.type == "rss":
        r = client.get(src.url)
        r.raise_for_status()
        items = parse_rss(r.content, src)
    else:
        r = client.get(f"https://old.reddit.com/r/{src.subreddit}/top/.json", params={"t": "day", "limit": 25})
        r.raise_for_status()
        items = parse_reddit(r.json(), src)
    if src.include_regex:
        items = [it for it in items if re.search(src.include_regex, f"{it['title']} {it.get('summary', '')}", re.I | re.S)]
    for it in items:
        it.update(source=src.name, weight=src.weight, use_images=src.use_images,
                  fetch_full_text=src.fetch_full_text)
    return items


def fetch_full_text(client: httpx.Client, url: str, limit: int = 6000) -> str:
    try:
        r = client.get(url)
        r.raise_for_status()
        body = re.search(r"(?is)<article.*?</article>", r.text)
        return strip_html(body.group(0) if body else r.text)[:limit]
    except Exception as e:  # статья не обязательна, хватит и анонса
        log.info("full text failed for %s: %s", url, e)
        return ""


def passes_filters(it: dict, ch: ChannelCfg) -> tuple[bool, str]:
    f = ch.filters
    text = f"{it['title']} {it.get('summary', '')}".lower()
    if ch.kind == "news" and len(it["title"]) < f.min_title_len:
        return False, "short title"
    if any(k.lower() in text for k in f.exclude_keywords):
        return False, "excluded keyword"
    if f.include_keywords and not any(k.lower() in text for k in f.include_keywords):
        return False, "no include keyword"
    if f.include_regex and not re.search(f.include_regex, text, re.I | re.S):
        return False, "no include regex"
    if f.exclude_regex and re.search(f.exclude_regex, text, re.I | re.S):
        return False, "excluded regex"
    return True, ""


def ingest(items: list[dict], ch: ChannelCfg, storage, dup_threshold: float = 0.6) -> int:
    """Кладёт новые материалы в базу, отбрасывая дубли по ссылке и по похожему заголовку."""
    import time
    recent = storage.recent_titles(ch.id)
    added = 0
    for it in items:
        it["key"] = hashlib.sha1(normalize_url(it["url"]).encode()).hexdigest()
        it["title_norm"] = normalize_title(it["title"])
        if it.get("published_ts") and it["published_ts"] < time.time() - ch.filters.max_age_hours * 3600:
            continue
        ok, reason = passes_filters(it, ch)
        if not ok:
            it.update(status="rejected", reason=reason)
        elif ch.kind == "news" and any(similarity(it["title_norm"], t) >= dup_threshold for t in recent):
            it.update(status="duplicate", reason="similar title")
        if storage.add_item(ch.id, it) and it.get("status", "new") == "new":
            recent.append(it["title_norm"])
            added += 1
    return added


def collect(ch: ChannelCfg, storage, client: httpx.Client | None = None) -> int:
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
    total = 0
    try:
        for src in ch.sources:
            try:
                total += ingest(fetch_source(client, src), ch, storage)
            except Exception as e:
                log.warning("[%s] source %s failed: %s", ch.id, src.name, e)
    finally:
        if own:
            client.close()
    storage.expire_old(ch.id, ch.filters.max_age_hours)
    return total
