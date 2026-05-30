"""DB: schema extensions — trusted_identities, chats, reply_to_telegram_msg_id."""
import tg_local_client.db as db


def _reload_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "messages.db")
    monkeypatch.setattr(db, "MEDIA_DIR", tmp_path / "media")


# ---------------------------------------------------------------------------
# trusted_identities + trusted_user_ids helper
# ---------------------------------------------------------------------------

def test_trusted_identities_schema_created(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute(
        "INSERT INTO trusted_identities "
        "(user_id, identity, trusted_at) VALUES (1, 'james', 1000)"
    )
    row = conn.execute(
        "SELECT identity FROM trusted_identities WHERE user_id = 1"
    ).fetchone()
    conn.close()
    assert row["identity"] == "james"


def test_trusted_user_ids_all(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO trusted_identities "
                 "(user_id, identity, trusted_at) VALUES (10, 'james', 1)")
    conn.execute("INSERT INTO trusted_identities "
                 "(user_id, identity, trusted_at) VALUES (20, 'glen', 1)")
    conn.close()
    ids = db.trusted_user_ids()
    assert set(ids) == {10, 20}


def test_trusted_user_ids_filtered(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO trusted_identities "
                 "(user_id, identity, trusted_at) VALUES (10, 'james', 1)")
    conn.execute("INSERT INTO trusted_identities "
                 "(user_id, identity, trusted_at) VALUES (20, 'glen', 1)")
    conn.close()
    ids = db.trusted_user_ids(["james"])
    assert ids == [10]


def test_trusted_user_ids_empty_db(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    db.connect().close()
    assert db.trusted_user_ids() == []


# ---------------------------------------------------------------------------
# chats table + note_chat
# ---------------------------------------------------------------------------

def test_note_chat_inserts(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    db.note_chat(-100123, "My Group", "supergroup", 1000)
    conn = db.connect()
    row = conn.execute("SELECT * FROM chats WHERE chat_id = -100123").fetchone()
    conn.close()
    assert row["title"] == "My Group"
    assert row["type"] == "supergroup"
    assert row["first_seen_ts"] == 1000
    assert row["last_seen_ts"] == 1000


def test_note_chat_updates_title(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    db.note_chat(-100123, "Old Name", "supergroup", 1000)
    db.note_chat(-100123, "New Name", "supergroup", 2000)
    conn = db.connect()
    row = conn.execute("SELECT * FROM chats WHERE chat_id = -100123").fetchone()
    conn.close()
    assert row["title"] == "New Name"
    assert row["first_seen_ts"] == 1000   # preserved
    assert row["last_seen_ts"] == 2000    # updated


# ---------------------------------------------------------------------------
# reply_to_telegram_msg_id in messages schema
# ---------------------------------------------------------------------------

def test_reply_to_field_persists(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute(
        "INSERT INTO messages "
        "(telegram_msg_id, chat_id, text, ts, direction, reply_to_telegram_msg_id) "
        "VALUES (5, -100, 'reply text', 1, 'in', 3)"
    )
    row = conn.execute(
        "SELECT reply_to_telegram_msg_id FROM messages WHERE telegram_msg_id = 5"
    ).fetchone()
    conn.close()
    assert row["reply_to_telegram_msg_id"] == 3


def test_reply_to_field_null_for_non_reply(tmp_path, monkeypatch):
    _reload_data_dir(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute(
        "INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
        "VALUES (6, -100, 'plain msg', 1, 'in')"
    )
    row = conn.execute(
        "SELECT reply_to_telegram_msg_id FROM messages WHERE telegram_msg_id = 6"
    ).fetchone()
    conn.close()
    assert row["reply_to_telegram_msg_id"] is None
