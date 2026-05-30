"""SQLite + per-channel JSONL store for the generic single-bot Telegram client.

One bot, no registry, no managed-bot tables. Inbound + outbound messages land in
SQLite and in per-channel JSONL files (channels/<chat_id>.jsonl) — the same shape
the dex-tg fabric uses, so the flat `tail` monitor works unchanged.

Also stores:
  * trusted_identities — durable user_id → logical identity bindings, per-bot.
  * chats             — human-readable chat titles and types, self-populating.

Data dir resolution (so two bots on one machine never collide):
  * TG_LOCAL_DATA env var, if set, wins.
  * else ~/.local/share/tg-local/<bot_slug>/ — the slug comes from config so each
    cloned client gets its own isolated store.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .config import load_config


def _data_dir() -> Path:
    """Resolve the data dir lazily so tests can monkeypatch and config drives the
    per-slug default."""
    override = os.environ.get("TG_LOCAL_DATA")
    if override:
        return Path(override)
    slug = (load_config().get("bot_slug") or "default").strip() or "default"
    return Path.home() / ".local/share/tg-local" / slug


# Module-level handles. Resolved at import; tests monkeypatch these directly.
DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "messages.db"
MEDIA_DIR = DATA_DIR / "media"


def channel_jsonl_path(chat_id: int) -> Path:
    """Per-channel JSONL file path for a chat_id (created lazily). One file per
    channel — a DM is just a chat with its own chat_id, handled uniformly."""
    p = DATA_DIR / "channels" / f"{chat_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_msg_id INTEGER,
  chat_id INTEGER NOT NULL,
  from_user_id INTEGER,
  from_username TEXT,
  from_first_name TEXT,
  text TEXT,
  ts INTEGER NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  read_at INTEGER,
  media_type TEXT,
  media_file_id TEXT,
  media_file_unique_id TEXT,
  media_mime_type TEXT,
  media_file_size INTEGER,
  media_local_path TEXT,
  message_thread_id INTEGER,
  reply_to_telegram_msg_id INTEGER,
  UNIQUE(telegram_msg_id, chat_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_unread ON messages(read_at) WHERE read_at IS NULL AND direction = 'in';
"""

# Additive column migrations for older databases (run best-effort on every connect).
_MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN reply_to_telegram_msg_id INTEGER",
]

# trusted_identities — durable binding of Telegram user_ids to a logical identity
# (e.g. "james"). Each client's data dir has its own copy; trust is per-bot.
_MIGRATIONS.append("""CREATE TABLE IF NOT EXISTS trusted_identities (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    identity TEXT NOT NULL,
    trusted_at INTEGER NOT NULL,
    trusted_by_user_id INTEGER,
    note TEXT
)""")

# chats — human-readable names for chats. Self-populates as traffic flows.
_MIGRATIONS.append("""CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    type TEXT,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts INTEGER NOT NULL
)""")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column/table already exists
    return conn


def now_ts() -> int:
    return int(time.time())


def write_channel_line(record: dict) -> None:
    """Append one record as a compact JSON line to its per-channel file."""
    ch_path = channel_jsonl_path(record["chat_id"])
    with ch_path.open("a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()


def note_chat(chat_id: int, title: Optional[str], chat_type: Optional[str],
              ts: int) -> None:
    """Upsert a chats row recording a chat's human-readable name and type.

    On insert, both first_seen_ts and last_seen_ts are set to ts. On conflict,
    title, type, and last_seen_ts are refreshed (titles can change over time)
    while first_seen_ts is preserved.
    """
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO chats (chat_id, title, type, first_seen_ts, last_seen_ts)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 title = excluded.title,
                 type = excluded.type,
                 last_seen_ts = excluded.last_seen_ts""",
            (chat_id, title, chat_type, ts, ts),
        )
    finally:
        conn.close()


def trusted_user_ids(identities: Optional[list] = None) -> list:
    """Return Telegram user_ids bound to trusted identities.

    identities: allow-list of identity labels (e.g. ["james"]). If None or empty,
    returns every trusted user_id. The tail addressing layer uses this to translate
    logical identity names into concrete numeric user_ids for the in-process filter.

    Returns a de-duplicated list of ints (insertion order not guaranteed).
    """
    conn = connect()
    try:
        if identities:
            placeholders = ",".join("?" * len(identities))
            rows = conn.execute(
                f"SELECT DISTINCT user_id FROM trusted_identities "
                f"WHERE identity IN ({placeholders})",
                tuple(identities),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM trusted_identities"
            ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]
