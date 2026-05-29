"""Single-bot aiogram long-poller for the generic Telegram client.

Owns ONE token (from config.resolve_token), long-polls Telegram, and appends each
inbound message to SQLite + a per-channel JSONL file (the same shape the dex-tg
fabric uses, so the flat tail monitor works). No control socket, no registry, no
fabric.

This module exposes:
  * build_inbound_record(msg) — pure function, builds the JSONL/DB record (tested).
  * persist_inbound(record)   — write to SQLite + per-channel JSONL, dedup-safe.
  * make_bot(token)           — construct the aiogram Bot.
  * poll(bot)                 — run the dispatcher; called from a background task.
"""
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from .db import connect, now_ts, write_channel_line

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


def build_inbound_record(msg: Message) -> dict:
    """Build the inbound record dict from an aiogram Message.

    `ts` uses the message's own send time when available (msg.date), falling back
    to now — so the stored timestamp is authoritative, not processing time.
    """
    text = msg.text or msg.caption or ""
    ts = int(msg.date.timestamp()) if getattr(msg, "date", None) else now_ts()
    record = {
        "telegram_msg_id": msg.message_id,
        "chat_id": msg.chat.id,
        "from_user_id": msg.from_user.id if msg.from_user else None,
        "from_username": msg.from_user.username if msg.from_user else None,
        "from_first_name": msg.from_user.first_name if msg.from_user else None,
        "text": text,
        "ts": ts,
        "message_thread_id": getattr(msg, "message_thread_id", None),
        **_media_info(msg),
    }
    return record


def persist_inbound(record: dict) -> Optional[int]:
    """Insert an inbound record into SQLite (dedup on telegram_msg_id+chat_id) and,
    if newly inserted, append it to the per-channel JSONL. Returns the DB row id, or
    None if it was a duplicate."""
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO messages
               (telegram_msg_id, chat_id, from_user_id, from_username,
                from_first_name, text, ts, direction,
                media_type, media_file_id, media_file_unique_id,
                media_mime_type, media_file_size, message_thread_id)
               VALUES (:telegram_msg_id, :chat_id, :from_user_id, :from_username,
                       :from_first_name, :text, :ts, 'in',
                       :media_type, :media_file_id, :media_file_unique_id,
                       :media_mime_type, :media_file_size, :message_thread_id)""",
            record,
        )
        if not cur.rowcount:
            return None
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
            """INSERT OR IGNORE INTO messages
               (telegram_msg_id, chat_id, text, ts, direction)
               VALUES (?, ?, ?, ?, 'out')""",
            (telegram_msg_id, chat_id, text, now_ts()),
        )
        row_id = cur.lastrowid
    finally:
        conn.close()
    return {"telegram_msg_id": telegram_msg_id, "db_id": row_id, "chat_id": chat_id}


def make_bot(token: str) -> Bot:
    return Bot(token=token)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message()
    async def on_message(msg: Message) -> None:  # noqa: ANN202
        persist_inbound(build_inbound_record(msg))

    return dp


async def poll(bot: Bot) -> None:
    """Long-poll this bot forever, persisting inbound messages. Runs as a task."""
    dp = build_dispatcher()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_webhook failed: %s", exc)
    await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
