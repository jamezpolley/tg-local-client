"""Poller: record building + persistence to per-channel JSONL."""
import json
from types import SimpleNamespace

import tg_local_client.db as db
import tg_local_client.listener as listener


def _reload_data_dir(tmp_path, monkeypatch, bot_slug="test"):
    """Point the db module at a temp data dir."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "messages.db")
    monkeypatch.setattr(db, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(db, "BOT_SLUG", bot_slug)
    import tg_local_client.listener as _listener
    monkeypatch.setattr(_listener, "BOT_SLUG", bot_slug)


def _fake_message(text="hello", chat_id=-100123, msg_id=1, reply_to_id=None,
                  quote_text=None, quote_is_manual=None):
    reply_to = (SimpleNamespace(message_id=reply_to_id)
                if reply_to_id is not None else None)
    quote = (SimpleNamespace(text=quote_text, is_manual=quote_is_manual)
             if quote_text is not None else None)
    return SimpleNamespace(
        message_id=msg_id,
        chat=SimpleNamespace(id=chat_id, title="Test Chat", type="supergroup"),
        from_user=SimpleNamespace(id=42, username="someone", first_name="Some"),
        text=text,
        caption=None,
        date=SimpleNamespace(timestamp=lambda: 1700000000.0),
        message_thread_id=None,
        reply_to_message=reply_to,
        quote=quote,
        photo=None, document=None, voice=None, audio=None, video=None,
        video_note=None, animation=None, sticker=None,
    )


def test_build_inbound_record_uses_message_send_time():
    rec = listener.build_inbound_record(_fake_message(text="hi"))
    assert rec["text"] == "hi"
    assert rec["chat_id"] == -100123
    assert rec["from_username"] == "someone"
    # ts comes from msg.date, not processing time.
    assert rec["ts"] == 1700000000


def test_build_inbound_record_captures_reply_to():
    rec = listener.build_inbound_record(_fake_message(text="reply", reply_to_id=7))
    assert rec["reply_to_telegram_msg_id"] == 7


def test_build_inbound_record_reply_to_null_for_plain():
    rec = listener.build_inbound_record(_fake_message(text="plain"))
    assert rec["reply_to_telegram_msg_id"] is None


def test_build_inbound_record_captures_quote():
    rec = listener.build_inbound_record(_fake_message(
        text="my reply", reply_to_id=7,
        quote_text="the exact bit I meant", quote_is_manual=True))
    assert rec["quote_text"] == "the exact bit I meant"
    assert rec["quote_is_manual"] is True


def test_build_inbound_record_quote_null_when_absent():
    rec = listener.build_inbound_record(_fake_message(text="plain"))
    assert rec["quote_text"] is None
    assert rec["quote_is_manual"] is None


def test_persist_inbound_stores_and_surfaces_quote(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    rec = listener.build_inbound_record(_fake_message(
        msg_id=11, quote_text="snippet", quote_is_manual=True))
    listener.persist_inbound(rec)
    # DB column round-trips (bool stored as 1).
    conn = db.connect()
    row = conn.execute(
        "SELECT quote_text, quote_is_manual FROM messages WHERE telegram_msg_id = 11"
    ).fetchone()
    conn.close()
    assert row["quote_text"] == "snippet"
    assert row["quote_is_manual"] == 1
    # And it lands in the per-channel JSONL the tail follows.
    line = json.loads((tmp_path / "channels" / "-100123.jsonl").read_text().strip())
    assert line["quote_text"] == "snippet"
    assert line["quote_is_manual"] is True


def test_persist_inbound_writes_channel_jsonl(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    rec = listener.build_inbound_record(_fake_message(text="watch me", msg_id=7))
    row_id = listener.persist_inbound(rec)
    assert row_id is not None

    ch_file = tmp_path / "channels" / "-100123.jsonl"
    assert ch_file.exists()
    lines = ch_file.read_text().splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert written["text"] == "watch me"
    assert written["direction"] == "in"
    assert written["id"] == row_id


def test_persist_inbound_stores_reply_to(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    rec = listener.build_inbound_record(_fake_message(msg_id=10, reply_to_id=3))
    row_id = listener.persist_inbound(rec)
    conn = db.connect()
    row = conn.execute("SELECT reply_to_telegram_msg_id FROM messages WHERE id = ?",
                       (row_id,)).fetchone()
    conn.close()
    assert row["reply_to_telegram_msg_id"] == 3


def test_persist_inbound_stores_all_copies(tmp_path, monkeypatch):
    """Append-only: same telegram_msg_id stored twice (e.g. edit or duplicate delivery)."""
    _reload_data_dir(tmp_path, monkeypatch)
    rec = listener.build_inbound_record(_fake_message(msg_id=99))
    id1 = listener.persist_inbound(rec)
    id2 = listener.persist_inbound(rec)
    assert id1 is not None
    assert id2 is not None
    assert id1 != id2
    ch_file = tmp_path / "channels" / "-100123.jsonl"
    assert len(ch_file.read_text().splitlines()) == 2


def test_persist_inbound_stores_both_bot_slugs(tmp_path, monkeypatch):
    """Two bots sharing a data dir each get their own row."""
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="bot-a")
    rec_a = listener.build_inbound_record(_fake_message(msg_id=50))
    assert listener.persist_inbound(rec_a) is not None

    monkeypatch.setattr(db, "BOT_SLUG", "bot-b")
    monkeypatch.setattr(listener, "BOT_SLUG", "bot-b")
    rec_b = listener.build_inbound_record(_fake_message(msg_id=50))
    assert listener.persist_inbound(rec_b) is not None


def test_build_inbound_record_includes_bot_slug(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="my-bot")
    rec = listener.build_inbound_record(_fake_message(text="hi"))
    assert rec["bot_slug"] == "my-bot"


def test_chat_display_name_from_title():
    chat = SimpleNamespace(title="My Group", first_name=None, last_name=None,
                           username=None, id=-100)
    assert listener._chat_display_name(chat) == "My Group"


def test_chat_display_name_from_first_last():
    chat = SimpleNamespace(title=None, first_name="John", last_name="Doe",
                           username=None, id=1)
    assert listener._chat_display_name(chat) == "John Doe"


def test_chat_display_name_from_username():
    chat = SimpleNamespace(title=None, first_name=None, last_name=None,
                           username="johndoe", id=1)
    assert listener._chat_display_name(chat) == "@johndoe"
