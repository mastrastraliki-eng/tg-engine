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


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """Тесты не ходят в сеть за og:image и не пишут карточки в настоящую data/."""
    monkeypatch.setattr(sources, "fetch_og_image", lambda c, u: None)
    monkeypatch.setenv("TG_CARDS_DIR", str(tmp_path))


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

    def send_post(self, chat_id, text, image=None, reply_markup=None, fallback_image=None):
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
        def rewrite(self, ch, item, full_text="", recent=()):
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
    assert {"crypto_news", "ua_news", "alerts"} <= set(chs)
    tokens = [c.bot_token_env for c in chs.values()]
    assert len(tokens) == len(set(tokens)), "у каждого канала свой бот"


TG_PAGE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="air_alert_ua/101">
<div class="tgme_widget_message_text js-message_text" dir="auto"><i class="emoji" style="x"><b>🔴</b></i> <b>22:31 Повітряна тривога в Київська область.</b><br/>Слідкуйте за подальшими повідомленнями.<br/><a href="?q=%23%D0%9A">#Київська_область</a></div>
<div class="tgme_widget_message_footer compact js-message_footer"><time datetime="2026-10-20T19:31:00+00:00" class="time">22:31</time></div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="air_alert_ua/102">
<div class="tgme_widget_message_text js-message_text" dir="auto">🟢 22:50 Відбій тривоги в Київська область.</div>
<div class="tgme_widget_message_footer compact js-message_footer"><time datetime="2026-10-20T19:50:00+00:00" class="time">22:50</time></div></div></div>
"""


def test_parse_telegram_page():
    src = SourceCfg(type="telegram", channel="@air_alert_ua")
    items = sources.parse_telegram(TG_PAGE, src)
    assert [i["url"] for i in items] == ["https://t.me/air_alert_ua/101", "https://t.me/air_alert_ua/102"]
    assert items[0]["body"].startswith("🔴 <b>22:31 Повітряна тривога в Київська область.</b>\nСлідкуйте")
    assert items[0]["title"].startswith("🔴 22:31 Повітряна тривога")


def test_relay_skips_history_then_posts_verbatim(monkeypatch):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@alerts")
    FakeBot.sent.clear()
    ch = make_channel(kind="relay", sources=[SourceCfg(type="telegram", channel="air_alert_ua", name="Повітряна тривога")],
                      filters={"max_age_hours": 100000}, style={"language": "uk"})
    st = Storage(":memory:")
    eng = Engine(Settings(), {ch.id: ch}, st, FakeRewriter(), bot_factory=FakeBot)
    page = {"html": TG_PAGE}
    monkeypatch.setattr(sources, "fetch_source", lambda c, s: sources.parse_telegram(page["html"], s) and
                        [dict(it, source=s.name, weight=1) for it in sources.parse_telegram(page["html"], s)])
    assert eng.relay(ch) == 0 and FakeBot.sent == []          # старые посты не публикуем
    page["html"] = TG_PAGE.replace("air_alert_ua/102", "air_alert_ua/103").replace("22:50", "23:10")
    assert eng.relay(ch) == 1
    text = FakeBot.sent[0][2]
    assert text.startswith("🟢 23:10 Відбій тривоги в Київська область.")
    assert 'Джерело: <a href="https://t.me/air_alert_ua/103">Повітряна тривога</a>' in text


def test_market_summary_uses_api_numbers():
    from engine.market import build_summary
    coins = [
        {"id": "bitcoin", "symbol": "btc", "current_price": 67123.4, "price_change_percentage_24h": 2.345},
        {"id": "ethereum", "symbol": "eth", "current_price": 2456.78, "price_change_percentage_24h": -1.2},
        {"id": "tether", "symbol": "usdt", "current_price": 1.0, "price_change_percentage_24h": 0.01},
        {"id": "pepe", "symbol": "pepe", "current_price": 0.00001234, "price_change_percentage_24h": 15.0},
        {"id": "doge", "symbol": "doge", "current_price": 0.12, "price_change_percentage_24h": -8.0},
    ]
    text = build_summary(coins, ["bitcoin", "ethereum"], movers=1)
    assert "<b>BTC</b> $67 123  🟢 +2.3%" in text
    assert "<b>ETH</b> $2 457  🔴 -1.2%" in text
    assert "PEPE 🟢 +15.0%" in text and "DOGE 🔴 -8.0%" in text
    assert "USDT" not in text


def test_telegram_reply_quote_ignored():
    page = TG_PAGE.replace('<div class="tgme_widget_message_text js-message_text" dir="auto">🟢',
                           '<div class="tgme_widget_message_reply"><div class="tgme_widget_message_text js-message_reply_text">'
                           'старий пост</div></div><div class="tgme_widget_message_text js-message_text" dir="auto">🟢')
    items = sources.parse_telegram(page, SourceCfg(type="telegram", channel="air_alert_ua"))
    assert items[1]["body"].startswith("🟢 22:50") and "старий" not in items[1]["body"]


def test_source_link_without_utm():
    from engine.rewriter import finalize
    text = finalize(make_channel(), "<b>x</b>", "https://cointelegraph.com/news/a?utm_source=rss&id=3")
    assert 'href="https://cointelegraph.com/news/a?id=3"' in text


def test_recent_headlines_passed_to_llm(monkeypatch):
    seen = {}

    class Spy(FakeRewriter):
        def rewrite(self, ch, item, full_text="", recent=()):
            seen.setdefault("recent", list(recent))
            return super().rewrite(ch, item, full_text, recent)
    ch = make_channel()
    eng, st = setup_engine(ch, rewriter=Spy())
    st.add_post(ch.id, "<b>Атака на Київ</b>\n\nтекст", "published")
    eng.prepare(ch)
    assert seen["recent"] == ["Атака на Київ"]


def test_cards_render(tmp_path):
    from PIL import Image
    from engine import cards
    p = cards.title_card("Атака дронів на Київ: 21 поранений 🚀", "Новини України", "#0057b7", "", tmp_path)
    assert Image.open(p).size == (1280, 720)
    assert cards.alert_kind("🟢 Відбій тривоги в Київська область")[0] == "clear"
    assert cards.alert_kind("🔴 Повітряна тривога в Київська область")[0] == "alert"
    assert cards.alert_kind("Реактивний БпЛА курсом на Луцьк")[0] == "threat"
    a = cards.alert_card("Повітряна тривога", "x", tmp_path)
    assert a == cards.alert_card("Повітряна тривога в Сумській", "x", tmp_path)  # шаблон переиспользуется


def test_post_gets_card_when_source_has_no_photo(monkeypatch, tmp_path):
    ch = make_channel()
    eng, st = setup_engine(ch)
    eng.s.cards_dir = str(tmp_path)
    monkeypatch.setattr(sources, "fetch_og_image", lambda c, u: None)
    eng.prepare(ch)
    post = st.db.execute("SELECT image, fallback_image FROM posts").fetchone()
    assert post["image"].endswith(".png") and post["fallback_image"] is None


def test_telegram_video_parsed():
    page = TG_PAGE.replace('<div class="tgme_widget_message_text js-message_text" dir="auto">🟢',
                           '<video class="tgme_widget_message_video" src="https://cdn.t.me/v.mp4"></video>'
                           '<div class="tgme_widget_message_text js-message_text" dir="auto">🟢')
    items = sources.parse_telegram(page, SourceCfg(type="telegram", channel="x"))
    assert items[1]["image"] == "video:https://cdn.t.me/v.mp4"


def test_imagegen_falls_back_to_next_service(monkeypatch, tmp_path):
    from engine import imagegen
    calls = []
    monkeypatch.setitem(imagegen.PROVIDERS, "cloudflare", lambda c, p, m: calls.append("cf"))
    monkeypatch.setitem(imagegen.PROVIDERS, "pollinations", lambda c, p, m: calls.append("pol") or b"\xff\xd8\xffjpg")
    path = imagegen.generate("a lighthouse", "m", tmp_path, client=object())
    assert calls == ["cf", "pol"] and path.endswith(".jpg")
