import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine import health, sources
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
    monkeypatch.setattr(health, "_check_keys", lambda c, paid=False: [])
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
        def rewrite(self, ch, item, full_text="", recent=(), fmt="", alt_fmt=""):
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
    tokens = [c.bot_token_env for c in chs.values() if c.enabled]
    assert len(tokens) == len(set(tokens)), "у каждого работающего канала свой бот"


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
        def rewrite(self, ch, item, full_text="", recent=(), fmt="", alt_fmt=""):
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
    monkeypatch.setattr(health, "_check_keys", lambda c, paid=False: [])
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


def test_formats_rotate_without_repeats():
    ch = make_channel(style={"formats": ["a", "b", "c"]})
    eng = Engine(Settings(), {ch.id: ch}, Storage(":memory:"), FakeRewriter(), bot_factory=FakeBot,
                 rng=random.Random(3))
    seq = [eng.next_format(ch) for _ in range(30)]
    assert set(seq) == {"a", "b", "c"}
    assert all(len({seq[i], seq[i + 1], seq[i + 2]}) == 3 for i in range(len(seq) - 2))


def test_editor_picks_and_polishes():
    from engine.rewriter import Rewriter
    calls = []

    class R(Rewriter):
        def __init__(self):
            pass

        def _call(self, system, user, schema):
            calls.append(system[:40])
            if "главный редактор" in system:
                assert "<variant n=\"2\">" in user
                return Draft(suitable=True, reason="", text="<b>Лучший</b>")
            return Draft(suitable=True, reason="", text="черновик", image_prompt="coin")
    ch = make_channel(style={"editor": True})
    d = R().rewrite(ch, {"title": "t", "source": "s"}, fmt="a", alt_fmt="b")
    assert d.text == "<b>Лучший</b>" and d.image_prompt == "coin" and len(calls) == 3


def test_health_report_once_a_day(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")
    monkeypatch.setenv("AB", "alerttok")
    monkeypatch.setenv("AC", "42")
    calls = []

    class CountBot(FakeBot):
        def call(self, method, **p):
            calls.append((self.token, method, p))
            return 7 if method == "getChatMemberCount" else {}
    ch = make_channel(username="chan")
    eng, st = setup_engine(ch)
    eng.bot_factory = CountBot
    eng.s.alert_bot_token_env, eng.s.alert_chat_id_env = "AB", "AC"
    morning = datetime(2026, 10, 21, 9, 5, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
    assert not health.daily(eng, morning - 3600)      # до 9:00 рано
    assert health.daily(eng, morning)
    assert not health.daily(eng, morning + 600)       # второй раз в тот же день не шлёт
    sent = [p["text"] for t, m, p in calls if m == "sendMessage"]
    assert len(sent) == 1 and "⚠" in sent[0] and "ни одного поста" in sent[0] and "подписчиков 7" in sent[0] and "запросов к ИИ" in sent[0]
    st.add_post(ch.id, "p", "queued")
    st.set_post_status(st.next_queued(ch.id)["id"], "published")
    text = health.build_report(eng)
    assert text.startswith("✅") and "(+0)" in text


def test_seed_publishes_burst_then_normal(monkeypatch):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")
    FakeBot.sent.clear()
    ch = make_channel(schedule={"posts_per_day": 1})
    eng, st = setup_engine(ch)
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
    for i in range(4):
        st.add_post(ch.id, f"p{i}", "queued")
    eng.seed(ch, 3, noon)
    assert eng.publish_due(ch, noon) is not None
    assert eng.publish_due(ch, noon + 60) is None                  # пауза между стартовыми постами
    assert eng.publish_due(ch, noon + 13 * 60) is not None          # дневной лимит не мешает
    assert eng.publish_due(ch, noon + 26 * 60) is not None
    assert st.get_state(ch.id, "seed_left") == "0"
    assert len(FakeBot.sent) == 3


def test_relay_seed_replays_only_fresh_summaries(monkeypatch):
    ch = make_channel(kind="relay", sources=[SourceCfg(type="telegram", channel="a"),
                                             SourceCfg(type="telegram", channel="gs", include_regex="втрати")])
    st = Storage(":memory:")
    eng = Engine(Settings(dry_run=True), {ch.id: ch}, st, FakeRewriter(), bot_factory=FakeBot)
    for i, src in enumerate(["@a", "@gs", "@gs"]):
        st.add_item(ch.id, {"key": f"k{i}", "title": f"t{i} втрати", "title_norm": f"t{i}", "url": f"https://t.me/x/{i}",
                            "source": src, "published_ts": time.time() - 3600})
    st.db.execute("UPDATE items SET status='rejected', reason='before start'")
    st.set_state(ch.id, "primed", "1")
    monkeypatch.setattr(sources, "collect", lambda c, s: 0)
    eng.seed(ch, 5)
    assert len(st.get_state(ch.id, "seed_items").split(",")) == 2   # тревоги из @a не повторяем


def test_no_preparing_at_night(monkeypatch):
    ch = make_channel(schedule={"active_hours": [7, 22]})
    eng, st = setup_engine(ch)
    called = []
    monkeypatch.setattr(eng, "collect", lambda c: called.append("c"))
    monkeypatch.setattr(eng, "prepare", lambda c: called.append("p"))
    tz = ZoneInfo("Europe/Kyiv")
    eng.tick(datetime(2026, 10, 21, 2, 0, tzinfo=tz).timestamp())
    assert called == []
    eng.tick(datetime(2026, 10, 21, 6, 30, tzinfo=tz).timestamp())   # за час до начала уже готовим
    assert called == ["c", "p"]


def test_seed_prepares_at_night(monkeypatch):
    ch = make_channel(schedule={"active_hours": [9, 22]})
    eng, st = setup_engine(ch)
    called = []
    monkeypatch.setattr(eng, "collect", lambda c: called.append("c"))
    monkeypatch.setattr(eng, "prepare", lambda c: called.append("p"))
    now = datetime(2026, 10, 21, 22, 6, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
    eng.seed(ch, 1, now)
    eng.tick(now)
    assert called == ["c", "p"]


def test_repeat_headline_detected():
    from engine.core import is_repeat
    recent = ["15 років за спробу підірвати залізницю"]
    assert is_repeat("Суд дав 15 років чоловіку, який намагався підірвати залізницю", recent)
    assert not is_repeat("Україна отримала від США нові ракети", ["Росія атакувала Україну дронами вночі"])


def test_free_llm_rotates_keys(monkeypatch):
    import httpx
    from engine.llm import FreeLLM
    monkeypatch.setenv("GROQ_API_KEY", "g1,g2")
    monkeypatch.setenv("GEMINI_API_KEY", "m1")
    seen = []

    def handler(req):
        key = req.headers["authorization"].split()[1]
        seen.append(key)
        if key == "g1":
            return httpx.Response(429, text="daily limit")
        if key == "g2":
            return httpx.Response(401, text="bad key")
        return httpx.Response(200, json={"choices": [{"message": {"content":
            'вот: {"suitable": true, "reason": "ok", "text": "<b>Т</b>\\n\\nтекст"}'}}]})
    st = Storage(":memory:")
    llm = FreeLLM(st, ["groq", "gemini"], client=httpx.Client(transport=httpx.MockTransport(handler)))
    d, who = llm.complete("sys", "user", Draft)
    assert d.text.startswith("<b>Т</b>") and who.startswith("gemini") and seen == ["g1", "g2", "m1"]
    seen.clear()
    llm.complete("sys", "user", Draft)
    assert seen == ["m1"]  # ключи в лимите пропускаются


def test_template_when_no_llm(monkeypatch):
    from engine.rewriter import Rewriter
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    rw = Rewriter(Settings(), Storage(":memory:"))
    ua = make_channel(style={"language": "uk"})
    d = rw.rewrite(ua, {"title": "Україна отримала нові ракети", "summary": "Це важлива новина для всіх. " * 40,
                        "source": "s"})
    assert d.suitable and d.text.startswith("<b>Україна") and len(d.text) < 800
    d = rw.rewrite(ua, {"title": "Ukraine gets new missiles", "summary": "x", "source": "s"})
    assert not d.suitable


def test_night_posts_do_not_use_daily_limit():
    from engine.schedule import day_start_ts
    s = ScheduleCfg(active_hours=[7, 22], timezone="Europe/Kyiv")
    now = datetime(2026, 10, 21, 18, 0, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
    night = datetime(2026, 10, 21, 3, 30, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
    assert night < day_start_ts(now, s) < now


def test_promo_link_is_clickable():
    from engine.rewriter import channel_link, sanitize_html
    link = channel_link("trymai_kurs_ua")
    assert link == '<a href="https://t.me/trymai_kurs_ua">@trymai_kurs_ua</a>'
    assert channel_link("@trymai_kurs_ua") == link and sanitize_html(link) == link


def test_old_promo_link_fixed_once(monkeypatch):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")
    edits = []

    class EditBot(FakeBot):
        def call(self, method, **p):
            edits.append((method, p["message_id"], p["text"]))
    ch = make_channel()
    eng, st = setup_engine(ch)
    eng.bot_factory = EditBot
    pid = st.add_post(ch.id, "Текст\n\n👉 trymai_kurs_ua", "queued", kind="promo")
    st.set_post_status(pid, "published", message_id=29)
    eng.fix_promo_links()
    eng.fix_promo_links()
    assert len(edits) == 1 and edits[0][1] == 29 and 'href="https://t.me/trymai_kurs_ua"' in edits[0][2]


def test_profile_synced_once(monkeypatch, tmp_path):
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("C1", "@chan")
    photo = tmp_path / "a.png"
    photo.write_bytes(b"x")
    calls = []

    class PBot(FakeBot):
        def call(self, method, **p):
            calls.append(method)

        def set_chat_photo(self, chat_id, path):
            calls.append("setChatPhoto")
    ch = make_channel(profile={"title": "Нове", "description": "Опис", "photo": str(photo)})
    eng, st = setup_engine(ch)
    eng.bot_factory = PBot
    eng.sync_profiles()
    eng.sync_profiles()
    assert calls == ["setChatTitle", "setChatDescription", "setChatPhoto"]


def test_headline_skips_rubric_line():
    from engine.core import headline
    assert headline("⚡️ Терміново\n\n<b>Рада ухвалила бюджет на 2027 рік</b>\n\nтекст") == \
        "Рада ухвалила бюджет на 2027 рік"
    assert headline("<b>Коротко</b>") == "Коротко"


def test_detect_coin_first_mentioned():
    from engine.market import detect_coin
    assert detect_coin("Киты скупают <b>эфир</b>, а биткоин стоит") == ("ethereum", "ETH")
    assert detect_coin("SEC против Ripple: XRP растёт") == ("ripple", "XRP")
    assert detect_coin("Регуляторы ЕС приняли закон") is None


def _ohlc(n=48):
    t, p, out = NOW * 1000, 100.0, []
    for i in range(n):
        c = p * (1.01 if i % 3 else 0.99)
        out.append([t + i * 1800000, p, max(p, c) * 1.002, min(p, c) * 0.998, c])
        p = c
    return out


def test_chart_card_renders(tmp_path):
    from engine import cards
    from PIL import Image
    path = cards.chart_card("BTC", _ohlc(), "Крипто Пульс", "#f7931a", tmp_path)
    assert Image.open(path).size == (1280, 720)


def test_media_mix_rotates_and_prefers_video(monkeypatch):
    from engine import market
    monkeypatch.setattr(market, "fetch_ohlc", lambda c, cid, days=1: _ohlc())
    ch = make_channel(style={"images": "source_or_card", "media_mix": {"source": 1, "chart": 1}})
    eng = Engine(Settings(), {ch.id: ch}, Storage(":memory:"), FakeRewriter(), bot_factory=FakeBot,
                 rng=random.Random(1))
    d = Draft(suitable=True, reason="", text="<b>Биткоин обновил максимум</b>")
    img, fb = eng.pick_media(ch, d, "t", "Биткоин обновил максимум", "video:https://cdn/x.mp4")
    assert img == "video:https://cdn/x.mp4" and fb.endswith(".png")
    kinds = []
    for _ in range(12):
        img, fb = eng.pick_media(ch, d, "t", "Биткоин обновил максимум", "https://ex.com/p.jpg")
        kinds.append("source" if img == "https://ex.com/p.jpg" else "chart")
        assert fb  # всегда есть запасная картинка
    assert {"source", "chart"} == set(kinds)
    # без монеты в тексте график невозможен: берём фото из новости
    d2 = Draft(suitable=True, reason="", text="<b>Регуляторы ЕС приняли закон</b>")
    assert eng.pick_media(ch, d2, "t", "x", "https://ex.com/p.jpg")[0] == "https://ex.com/p.jpg"


def test_evening_digest_links_posts(monkeypatch):
    ch = make_channel(username="test_chan", digest={"enabled": True, "time": "21:00", "max_items": 3})
    st = Storage(":memory:")
    eng = Engine(Settings(), {ch.id: ch}, st, FakeRewriter(), bot_factory=FakeBot)
    monkey_now = datetime(2026, 10, 26, 21, 5, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
    for i in range(5):
        pid = st.add_post(ch.id, f"⚡️ Терміново\n\n<b>Новина номер {i} про важливе</b>", "published")
        st.db.execute("UPDATE posts SET message_id=?, published_ts=? WHERE id=?",
                      (100 + i, monkey_now - 3600 * (5 - i), pid))
    st.db.commit()
    monkeypatch.setenv("T1", "tok")
    monkeypatch.setenv("C1", "@c")
    assert eng.digest_due(ch, monkey_now)
    FakeBot.sent.clear()
    eng.digest(ch, monkey_now)
    text = FakeBot.sent[-1][2]
    assert text.count("▪️") == 3 and 'href="https://t.me/test_chan/100"' in text
    assert "Новина номер 0 про важливе" in text and "Терміново" not in text
    assert not eng.digest_due(ch, monkey_now + 60)
