"""Poller: record building + persistence to per-channel JSONL."""
import json
from types import SimpleNamespace

import tg_local_client.db as db
import tg_local_client.listener as listener


def _reload_data_dir(tmp_path, monkeypatch):
    """Point the db module at a temp data dir."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "messages.db")
    monkeypatch.setattr(db, "MEDIA_DIR", tmp_path / "media")


def _fake_message(text="hello", chat_id=-100123, msg_id=1, reply_to_id=None):
    reply_to = (SimpleNamespace(message_id=reply_to_id)
                if reply_to_id is not None else None)
    return SimpleNamespace(
        message_id=msg_id,
        chat=SimpleNamespace(id=chat_id, title="Test Chat", type="supergroup"),
        from_user=SimpleNamespace(id=42, username="someone", first_name="Some"),
        text=text,
        caption=None,
        date=SimpleNamespace(timestamp=lambda: 1700000000.0),
        message_thread_id=None,
        reply_to_message=reply_to,
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


def test_persist_inbound_dedupes(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    rec = listener.build_inbound_record(_fake_message(msg_id=99))
    assert listener.persist_inbound(rec) is not None
    # Same telegram_msg_id + chat_id → duplicate, dropped, no second jsonl line.
    assert listener.persist_inbound(rec) is None
    ch_file = tmp_path / "channels" / "-100123.jsonl"
    assert len(ch_file.read_text().splitlines()) == 1


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
