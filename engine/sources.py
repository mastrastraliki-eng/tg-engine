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


def fetch_source(client: httpx.Client, src: SourceCfg) -> list[dict]:
    if src.type == "rss":
        r = client.get(src.url)
        r.raise_for_status()
        items = parse_rss(r.content, src)
    else:
        r = client.get(f"https://www.reddit.com/r/{src.subreddit}/top/.json", params={"t": "day", "limit": 25})
        r.raise_for_status()
        items = parse_reddit(r.json(), src)
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
    if len(it["title"]) < f.min_title_len:
        return False, "short title"
    if any(k.lower() in text for k in f.exclude_keywords):
        return False, "excluded keyword"
    if f.include_keywords and not any(k.lower() in text for k in f.include_keywords):
        return False, "no include keyword"
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
        elif any(similarity(it["title_norm"], t) >= dup_threshold for t in recent):
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
