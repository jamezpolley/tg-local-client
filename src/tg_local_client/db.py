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
BOT_SLUG = (load_config().get("bot_slug") or "").strip()


def is_private_chat(chat_id: int) -> bool:
    """True when chat_id is a private (user) DM — positive ids.

    Telegram assigns positive ids to users and negative ids to groups/supergroups/
    channels. In a single-bot-per-datadir deployment (the common local-client case)
    the private-vs-group distinction is mainly for consistency with the fabric's
    bot-keyed scheme. The caller supplies bot_slug so two bots sharing a data dir
    would still get separate DM files.
    """
    return chat_id > 0


def bot_jsonl_path() -> Path:
    """Single per-bot JSONL file: channels/<bot_slug>.jsonl.

    All inbound messages (DMs + every group the bot is in) are appended here in
    arrival order. tg-local-tail watches this one file; per-channel files are kept
    as secondary copies for backward compatibility.
    """
    p = DATA_DIR / "channels" / f"{BOT_SLUG}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def channel_jsonl_path(chat_id: int, bot_slug: Optional[str] = None) -> Path:
    """Per-channel JSONL file path for a chat_id (created lazily).

    PRIVATE CHATS (positive chat_id): when bot_slug is given, the file is
    bot-keyed: channels/<chat_id>__<bot_slug>.jsonl.  This mirrors the fabric's
    scheme so two local-client bots sharing a data dir don't bleed DMs.  When
    bot_slug is None (legacy callers, single-bot reality) the old flat path is
    returned for backward compatibility.

    GROUP / SUPERGROUP / CHANNEL (negative chat_id): shared flat scheme unchanged —
    channels/<chat_id>.jsonl.
    """
    if is_private_chat(chat_id) and bot_slug:
        p = DATA_DIR / "channels" / f"{chat_id}__{bot_slug}.jsonl"
    else:
        p = DATA_DIR / "channels" / f"{chat_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def all_known_chat_ids() -> list[int]:
    """Return all chat_ids seen in the local store (from the messages table).

    Used by get_tail_command() with no channel args to live-derive the full set
    of channels rather than relying on the static group_chat_ids config seed.
    Returns a de-duplicated list in no guaranteed order.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM messages"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_slug TEXT NOT NULL DEFAULT '',
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
  quote_text TEXT,
  quote_is_manual INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_unread ON messages(read_at) WHERE read_at IS NULL AND direction = 'in';
CREATE INDEX IF NOT EXISTS idx_msg_lookup ON messages(telegram_msg_id, chat_id);
"""

# Additive column migrations for older databases (run best-effort on every connect).
_MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN reply_to_telegram_msg_id INTEGER",
    # Reply-quote: the specific snippet a human highlights when quote-replying
    # (Telegram's message.quote). quote_text = the highlighted excerpt;
    # quote_is_manual = 1 if the user manually selected it (vs auto-quoted).
    "ALTER TABLE messages ADD COLUMN quote_text TEXT",
    "ALTER TABLE messages ADD COLUMN quote_is_manual INTEGER",
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


def _migrate_add_bot_slug(conn: sqlite3.Connection) -> None:
    """Add bot_slug column and drop the UNIQUE dedup constraint (append-only design).

    SQLite cannot ALTER a UNIQUE constraint, so we rebuild the table. The new schema
    has no UNIQUE on messages — every delivery is appended, dedup happens at read time.
    Idempotent: returns early if bot_slug column already exists.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "bot_slug" in cols:
        return
    conn.executescript("""
        BEGIN;
        CREATE TABLE messages_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          bot_slug TEXT NOT NULL DEFAULT '',
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
          quote_text TEXT,
          quote_is_manual INTEGER
        );
        INSERT INTO messages_new SELECT id, '', telegram_msg_id, chat_id,
          from_user_id, from_username, from_first_name, text, ts, direction,
          read_at, media_type, media_file_id, media_file_unique_id, media_mime_type,
          media_file_size, media_local_path, message_thread_id,
          reply_to_telegram_msg_id, quote_text, quote_is_manual FROM messages;
        DROP TABLE messages;
        ALTER TABLE messages_new RENAME TO messages;
        CREATE INDEX IF NOT EXISTS idx_chat_ts ON messages(chat_id, ts);
        CREATE INDEX IF NOT EXISTS idx_unread ON messages(read_at)
          WHERE read_at IS NULL AND direction = 'in';
        CREATE INDEX IF NOT EXISTS idx_msg_lookup ON messages(telegram_msg_id, chat_id);
        COMMIT;
    """)
    # Backfill existing rows with this bot's slug (parameterized — avoids f-string SQL).
    conn.execute("UPDATE messages SET bot_slug = ? WHERE bot_slug = ''", (BOT_SLUG,))


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
    _migrate_add_bot_slug(conn)
    return conn


def now_ts() -> int:
    return int(time.time())


def write_channel_line(record: dict, bot_slug: Optional[str] = None) -> None:
    """Append one record to the per-bot file and the per-channel file.

    The per-bot file (channels/<bot_slug>.jsonl) is the primary tail target —
    every message from every chat lands here in arrival order.  The per-channel
    file is kept as a secondary copy for backward compatibility.
    """
    line = json.dumps(record) + "\n"
    # Primary: per-bot aggregated file (single file for tg-local-tail).
    bot_path = bot_jsonl_path()
    with bot_path.open("a") as f:
        f.write(line)
        f.flush()
    # Secondary: per-channel file (backward compat; kept but not tailed).
    ch_path = channel_jsonl_path(record["chat_id"], bot_slug)
    if ch_path != bot_path:
        with ch_path.open("a") as f:
            f.write(line)
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
