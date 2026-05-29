"""SQLite + per-channel JSONL store for the generic single-bot Telegram client.

One bot, no registry, no managed-bot or trusted-identity tables. Inbound + outbound
messages land in SQLite and in per-channel JSONL files (channels/<chat_id>.jsonl) —
the same shape the dex-tg fabric uses, so the flat `tail` monitor works unchanged.

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
  UNIQUE(telegram_msg_id, chat_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_unread ON messages(read_at) WHERE read_at IS NULL AND direction = 'in';
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now_ts() -> int:
    return int(time.time())


def write_channel_line(record: dict) -> None:
    """Append one record as a compact JSON line to its per-channel file."""
    ch_path = channel_jsonl_path(record["chat_id"])
    with ch_path.open("a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
