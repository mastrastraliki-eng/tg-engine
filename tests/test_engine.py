import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine import sources
from engine.config import ChannelCfg, ScheduleCfg, Settings, SourceCfg, load_channels
from engine.core import Engine
from engine.rewriter import Draft, FakeRewriter, sanitize_html
from engine.schedule import next_slot
from engine.storage import Storage
from engine.telegram import TelegramError

NOW = time.time()
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Bitcoin ETF sees record inflows this week</title><link>https://ex.com/a?utm_source=x</link>
<description>&lt;p&gt;Big &lt;b&gt;inflows&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(NOW - 3600))}</pubDate></item>
<item><title>Record inflows for Bitcoin ETF this week</title><link>https://ex.com/b</link>
<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(NOW - 3600))}</pubDate></item>
<item><title>Sponsored: new token presale starts now</title><link>https://ex.com/c</link></item>
<item><title>Ancient news from last month about Ethereum</title><link>https://ex.com/d</link>
<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(NOW - 40 * 86400))}</pubDate></item>
</channel></rss>"""


def make_channel(**kw):
    base = dict(id="c1", title="Test", niche="crypto", bot_token_env="T1", chat_id_env="C1",
                mode="auto", sources=[SourceCfg(type="rss", url="https://ex.com/rss")],
                filters={"exclude_keywords": ["sponsored"], "max_age_hours": 24})
    base.update(kw)
    return ChannelCfg(**base)


class FakeBot:
    sent = []

    def __init__(self, token):
        self.token = token

    def send_post(self, chat_id, text, image=None, reply_markup=None):
        FakeBot.sent.append((self.token, chat_id, text))
        return len(FakeBot.sent)


def test_normalize_url_strips_tracking():
    assert sources.normalize_url("http://www.Ex.com/a/?utm_source=x&id=2") == "https://ex.com/a?id=2"


def test_ingest_dedupes_and_filters():
    st = Storage(":memory:")
    ch = make_channel()
    items = sources.parse_rss(RSS, ch.sources[0])
    for it in items:
        it.update(source="t", weight=1)
    assert sources.ingest(items, ch, st) == 1
    rows = {r["title"]: r["status"] for r in st.db.execute("SELECT * FROM items")}
    assert rows["Bitcoin ETF sees record inflows this week"] == "new"
    assert rows["Record inflows for Bitcoin ETF this week"] == "duplicate"
    assert rows["Sponsored: new token presale starts now"] == "rejected"
    assert "Ancient news from last month about Ethereum" not in rows
    assert sources.ingest(items, ch, st) == 0  # повторный сбор ничего не добавляет


def test_sanitize_html():
    assert sanitize_html("<b>hi</b> <script>x</script> a<b") == "<b>hi</b> x a&lt;b"
    assert sanitize_html('<strong>x</strong><a href="javascript:1">y</a>') == "<b>x</b>y"
    assert sanitize_html("<i>open") == "<i>open</i>"


def test_schedule_stays_in_active_hours():
    s = ScheduleCfg(posts_per_day=6, active_hours=(8, 23), timezone="Europe/Kyiv")
    rng = random.Random(1)
    t = NOW
    for _ in range(200):
        t = next_slot(t, s, rng)
        h = datetime.fromtimestamp(t, ZoneInfo(s.timezone)).hour
        assert 8 <= h < 23


def setup_engine(ch, rewriter=None, dry=False):
    st = Storage(":memory:")
    for it in sources.parse_rss(RSS, ch.sources[0]):
        it.update(source="t", weight=1)
        sources.ingest([it], ch, st)
    eng = Engine(Settings(dry_run=dry), {ch.id: ch}, st, rewriter or FakeRewriter(), bot_factory=FakeBot)
    return eng, st


def test_full_flow_publishes_on_schedule(monkeypatch):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")
    FakeBot.sent.clear()
    ch = make_channel()
    eng, st = setup_engine(ch)
    assert eng.prepare(ch) == 1
    assert eng.publish_due(ch, NOW) is None          # первый вызов только планирует слот
    st.set_state(ch.id, "next_at", str(NOW - 1))
    assert eng.publish_due(ch, NOW) == 1
    assert FakeBot.sent[0][1] == "@chan"
    assert "https://ex.com/a" in FakeBot.sent[0][2]
    assert float(st.get_state(ch.id, "next_at")) > NOW


def test_review_mode_holds_posts(monkeypatch):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")
    ch = make_channel(mode="review")
    eng, st = setup_engine(ch)
    eng.prepare(ch)
    st.set_state(ch.id, "next_at", str(NOW - 1))
    assert eng.publish_due(ch, NOW) is None
    assert st.count_posts(ch.id, "pending_review") == 1


def test_unsuitable_item_rejected():
    class No(FakeRewriter):
        def rewrite(self, ch, item, full_text=""):
            return Draft(suitable=False, reason="off-topic", text="")
    ch = make_channel()
    eng, st = setup_engine(ch, rewriter=No())
    assert eng.prepare(ch) == 0
    assert st.db.execute("SELECT reason FROM items WHERE status='rejected' AND reason='off-topic'").fetchone()


def test_fatal_telegram_error_pauses_channel(monkeypatch):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")

    class Banned(FakeBot):
        def send_post(self, *a, **k):
            raise TelegramError(403, "Forbidden: bot was kicked from the channel chat")
    ch = make_channel()
    eng, st = setup_engine(ch)
    eng.bot_factory = Banned
    eng.prepare(ch)
    st.set_state(ch.id, "next_at", str(NOW - 1))
    eng.publish_due(ch, NOW)
    assert "kicked" in eng.paused(ch)


def test_daily_cap(monkeypatch):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")
    ch = make_channel(schedule={"posts_per_day": 1})
    eng, st = setup_engine(ch)
    for i in range(3):
        st.add_post(ch.id, f"p{i}", "queued")
    st.set_state(ch.id, "next_at", str(NOW - 1))
    eng.publish_due(ch, NOW)
    st.set_state(ch.id, "next_at", str(NOW - 1))
    assert eng.publish_due(ch, NOW) is None
    assert st.count_posts(ch.id, "published") == 1


def test_example_configs_load():
    chs = load_channels("channels")
    assert {"crypto_news", "ai_tools", "wow_facts"} <= set(chs)
    tokens = [c.bot_token_env for c in chs.values()]
    assert len(tokens) == len(set(tokens)), "у каждого канала свой бот"
