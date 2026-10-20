"""CLI: python -m engine <команда>"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import load_channels, load_settings
from .core import Engine
from .rewriter import FakeRewriter, Rewriter
from .storage import Storage
from .telegram import Bot, TelegramError


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="engine", description="Движок сети Telegram-каналов")
    ap.add_argument("--settings", default="settings.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="проверить конфиги, токены и доступ ботов к каналам")
    p = sub.add_parser("preview", help="собрать и показать посты, ничего не публикуя")
    p.add_argument("channel")
    p.add_argument("-n", type=int, default=3)
    p.add_argument("--fake", action="store_true", help="без вызова LLM")
    sub.add_parser("run", help="рабочий цикл")
    sub.add_parser("tick", help="один проход цикла (для cron)")
    p = sub.add_parser("resume", help="снять канал с паузы")
    p.add_argument("channel")
    sub.add_parser("stats", help="статистика постов")
    a = ap.parse_args(argv)

    load_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx пишет полный URL запроса, а в URL Bot API лежит токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = load_settings(a.settings)
    channels = load_channels(s.channels_dir)

    if a.cmd == "check":
        ok = True
        for ch in channels.values():
            line = f"{ch.id:16} {'on ' if ch.enabled else 'off'} {ch.mode:6} sources={len(ch.sources)}"
            if not ch.bot_token or not ch.chat_id:
                print(f"{line}  ✗ нет {ch.bot_token_env}/{ch.chat_id_env}")
                ok = ok and not ch.enabled
                continue
            try:
                bot = Bot(ch.bot_token)
                me = bot.get_me()
                member = bot.call("getChatMember", chat_id=ch.chat_id, user_id=me["id"])
                can = member.get("status") == "administrator" and member.get("can_post_messages", True)
                print(f"{line}  @{me['username']} {'✓ может постить' if can else '✗ не админ канала'}")
                ok = ok and can
            except TelegramError as e:
                print(f"{line}  ✗ {e}")
                ok = False
        tokens = [c.bot_token for c in channels.values() if c.enabled and c.bot_token]
        if len(tokens) != len(set(tokens)):
            print("! несколько каналов на одном боте: при бане бота пострадают все они")
        return 0 if ok else 1

    st = Storage(":memory:" if a.cmd == "preview" else s.db_path)

    if a.cmd == "preview":
        ch = channels[a.channel]
        ch.queue_target = a.n
        ch.mode = "auto"
        s.dry_run = True
        eng = Engine(s, channels, st, FakeRewriter() if a.fake else Rewriter(s, st))
        if ch.kind == "relay":
            from . import sources
            print(f"собрано новых материалов: {sources.collect(ch, st)}")
            rows = st.db.execute("SELECT * FROM items WHERE channel=? AND status='new' "
                                 "ORDER BY COALESCE(published_ts, fetched_ts) DESC LIMIT ?", (ch.id, a.n)).fetchall()
            for row in reversed(rows):
                print("─" * 60 + f"\n{eng.relay_text(ch, row)}\n")
            for row in st.db.execute("SELECT title, reason FROM items WHERE channel=? AND status='rejected' "
                                     "AND reason NOT IN ('too old') LIMIT 5", (ch.id,)):
                print(f"✗ {row['title'][:70]} — {row['reason']}")
            return 0
        if ch.market_summary.enabled:
            from . import market
            import httpx
            with httpx.Client(timeout=20) as c:
                print("─" * 60 + "\n" + market.build_summary(market.fetch_markets(c), ch.market_summary.coins,
                                                              ch.market_summary.movers, ch.style.language) + "\n")
        print(f"собрано новых материалов: {eng.collect(ch)}")
        eng.prepare(ch)
        for row in st.db.execute("SELECT * FROM posts WHERE channel=?", (ch.id,)):
            print("─" * 60 + f"\n{row['text']}\n" + (f"[картинка: {row['image']}]\n" if row["image"] else ""))
        for row in st.db.execute("SELECT title, reason FROM items WHERE channel=? AND status='rejected' "
                                 "AND reason NOT IN ('too old') LIMIT 10", (ch.id,)):
            print(f"✗ {row['title'][:70]} — {row['reason']}")
        return 0

    if a.cmd == "resume":
        st.set_state(a.channel, "paused", None)
        print(f"{a.channel}: пауза снята")
        return 0

    if a.cmd == "stats":
        for r in st.stats():
            print(f"{r['channel']:16} {r['status']:15} {r['n']}")
        return 0

    eng = Engine(s, channels, st, Rewriter(s, st))
    if a.cmd == "tick":
        eng.tick()
    else:
        eng.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
