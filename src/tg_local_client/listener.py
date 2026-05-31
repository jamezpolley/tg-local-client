"""Single-bot aiogram long-poller for the generic Telegram client.

Owns ONE token (from config.resolve_token), long-polls Telegram, and appends each
inbound message to SQLite + a per-channel JSONL file (the same shape the dex-tg
fabric uses, so the flat tail monitor works). No control socket, no registry, no
fabric.

This module exposes:
  * build_inbound_record(msg) — pure function, builds the JSONL/DB record (tested).
  * persist_inbound(record)   — write to SQLite + per-channel JSONL, dedup-safe.
  * record_outbound(...)      — persist an outbound message to SQLite.
  * make_bot(token)           — construct the aiogram Bot.
  * poll(bot)                 — run the dispatcher; called from a background task.
"""
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from .db import BOT_SLUG, connect, now_ts, note_chat, write_channel_line

log = logging.getLogger("tg-local-listener")

ALLOWED_UPDATES = ["message", "edited_message"]


def _media_info(msg: Message) -> dict:
    """Extract the dominant media descriptor from a message, if any."""
    if msg.photo:
        biggest = msg.photo[-1]
        return {"media_type": "photo", "media_file_id": biggest.file_id,
                "media_file_unique_id": biggest.file_unique_id,
                "media_file_size": biggest.file_size, "media_mime_type": None}
    if msg.document:
        return {"media_type": "document", "media_file_id": msg.document.file_id,
                "media_file_unique_id": msg.document.file_unique_id,
                "media_file_size": msg.document.file_size,
                "media_mime_type": msg.document.mime_type}
    for attr, kind in (("voice", "voice"), ("audio", "audio"), ("video", "video"),
                       ("video_note", "video_note"), ("animation", "animation"),
                       ("sticker", "sticker")):
        item = getattr(msg, attr, None)
        if item:
            return {"media_type": kind, "media_file_id": item.file_id,
                    "media_file_unique_id": item.file_unique_id,
                    "media_file_size": getattr(item, "file_size", None),
                    "media_mime_type": getattr(item, "mime_type", None)}
    return {"media_type": None, "media_file_id": None, "media_file_unique_id": None,
            "media_file_size": None, "media_mime_type": None}


def _chat_display_name(chat) -> str:
    """Compute a human-readable name for a Telegram chat.

    Groups/supergroups/channels carry a `.title`. Private DMs carry
    `.first_name`/`.last_name`/`.username` instead. Falls back through:
    title → "first last" → "@username" → str(chat_id).
    """
    title = getattr(chat, "title", None)
    if title:
        return title
    first = getattr(chat, "first_name", None)
    last = getattr(chat, "last_name", None)
    name = " ".join(p for p in (first, last) if p).strip()
    if name:
        return name
    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"
    return str(getattr(chat, "id", ""))


def build_inbound_record(msg: Message) -> dict:
    """Build the inbound record dict from an aiogram Message.

    `ts` uses the message's own send time when available (msg.date), falling back
    to now — so the stored timestamp is authoritative, not processing time.

    `reply_to_telegram_msg_id` is set when the message is a reply to another
    message, so the monitoring agent can thread correctly.

    `quote_text` captures the specific snippet a human highlights when
    quote-replying (Telegram's `msg.quote`). It's vital context — exactly the
    excerpt the human was pointing at; without it the agent only sees the whole
    replied-to message. `quote_is_manual` distinguishes a hand-picked selection
    from an auto-quote.
    """
    text = msg.text or msg.caption or ""
    ts = int(msg.date.timestamp()) if getattr(msg, "date", None) else now_ts()
    reply_to = getattr(msg, "reply_to_message", None)
    quote = getattr(msg, "quote", None)
    record = {
        "bot_slug": BOT_SLUG,
        "telegram_msg_id": msg.message_id,
        "chat_id": msg.chat.id,
        "from_user_id": msg.from_user.id if msg.from_user else None,
        "from_username": msg.from_user.username if msg.from_user else None,
        "from_first_name": msg.from_user.first_name if msg.from_user else None,
        "text": text,
        "ts": ts,
        "message_thread_id": getattr(msg, "message_thread_id", None),
        "reply_to_telegram_msg_id": reply_to.message_id if reply_to else None,
        "quote_text": quote.text if quote else None,
        "quote_is_manual": bool(quote.is_manual) if quote else None,
        **_media_info(msg),
    }
    return record


def persist_inbound(record: dict) -> int:
    """Append an inbound record to SQLite and the per-channel JSONL.

    Append-only: every delivery is stored, including edits and duplicate deliveries.
    Dedup (latest per telegram_msg_id) happens at read time in list_recent_messages.
    Returns the DB row id.
    """
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO messages
               (bot_slug, telegram_msg_id, chat_id, from_user_id, from_username,
                from_first_name, text, ts, direction,
                media_type, media_file_id, media_file_unique_id,
                media_mime_type, media_file_size, message_thread_id,
                reply_to_telegram_msg_id, quote_text, quote_is_manual)
               VALUES (:bot_slug, :telegram_msg_id, :chat_id, :from_user_id, :from_username,
                       :from_first_name, :text, :ts, 'in',
                       :media_type, :media_file_id, :media_file_unique_id,
                       :media_mime_type, :media_file_size, :message_thread_id,
                       :reply_to_telegram_msg_id, :quote_text, :quote_is_manual)""",
            record,
        )
        row_id = cur.lastrowid
    finally:
        conn.close()
    write_channel_line({"id": row_id, "direction": "in", **record})
    log.info("inbound id=%s chat=%s from=%s len=%d",
             row_id, record["chat_id"], record["from_username"], len(record["text"]))
    return row_id


def record_outbound(telegram_msg_id: int, chat_id: int, text: str) -> dict:
    """Persist an outbound (sent) message so list_recent_messages / the monitor see it."""
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO messages
               (bot_slug, telegram_msg_id, chat_id, text, ts, direction)
               VALUES (?, ?, ?, ?, ?, 'out')""",
            (BOT_SLUG, telegram_msg_id, chat_id, text, now_ts()),
        )
        row_id = cur.lastrowid
    finally:
        conn.close()
    return {"telegram_msg_id": telegram_msg_id, "db_id": row_id, "chat_id": chat_id}


def make_bot(token: str) -> Bot:
    return Bot(token=token)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    async def _handle(msg: Message) -> None:
        record = build_inbound_record(msg)
        persist_inbound(record)
        # Record the chat's human-readable name + type so list_known_chats can
        # surface it without a side-channel lookup. Runs on every capture (titles
        # can change over time). Best-effort — never raises into the poller.
        try:
            note_chat(msg.chat.id, _chat_display_name(msg.chat),
                      getattr(msg.chat, "type", None), record["ts"])
        except Exception as exc:  # noqa: BLE001
            log.warning("note_chat failed: %s", exc)

    dp.message()(_handle)
    # Edits are appended as new rows (same telegram_msg_id, updated text).
    # list_recent_messages deduplicates at read time, showing the latest version.
    dp.edited_message()(_handle)

    return dp


async def poll(bot: Bot) -> None:
    """Long-poll this bot forever, persisting inbound messages. Runs as a task."""
    dp = build_dispatcher()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_webhook failed: %s", exc)
    await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
