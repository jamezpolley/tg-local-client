"""Single-process MCP + poller for the generic local Telegram client.

ONE process: the stdio MCP server owns the bot token, runs a background asyncio
task that long-polls Telegram (appending inbound messages to per-channel JSONL +
SQLite), and serves a full tool surface matching the dex-tg fabric's single-agent
tool set. No control socket, no registry, no fabric internals.

When the agent's Claude session (and thus this MCP) is up, it's listening; when it
closes, polling stops — that's fine. Telegram is the only shared substrate.

Everything bot-specific (server name, which group(s) to watch, which env var holds
the token) comes from config.local.json — see config.py.

## Tool set (single-bot adaption of the fabric tool surface)

Sending:
  send_message          — text with optional parse_mode / reply_to / thread
  stream_message_draft  — send-or-edit: first call sends, subsequent calls edit
  send_typing           — one-shot 5-second typing burst
  start_typing          — self-refreshing typing loop; auto-stops on send_message
  stop_typing           — cancel a start_typing loop early
  react_to_message    — set/clear a reaction emoji on a message
  edit_message        — edit text of a message this bot sent
  delete_message      — delete a message this bot sent
  send_photo          — send a photo from the local filesystem
  send_document       — send any file as a document

Receiving:
  list_recent_messages — list inbound messages from the local store
  list_known_chats     — list all chats the client has seen
  mark_read            — mark messages read by local DB id
  download_media       — download a media attachment from a stored message

Monitoring:
  get_tail_command     — build the flat tg-local-tail command for Bash+Monitor

Identity / trust:
  lookup_identity      — resolve a user_id to a logical identity
  list_trusted_identities — enumerate trust bindings
  trust_identity       — add/update a trust binding
  untrust_identity     — remove a trust binding

Forum topics:
  create_forum_topic   — create a new forum topic
  edit_forum_topic     — edit name/icon of a topic
  close_forum_topic    — close a topic
  reopen_forum_topic   — reopen a closed topic

Meta:
  client_version       — fingerprint: loaded version + registered tools
"""
import asyncio
import contextlib
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

from aiogram.types import FSInputFile, ReactionTypeEmoji

from fastmcp import FastMCP

from ._version import CLIENT_VERSION
from .config import default_chat_id, load_config, resolve_token, token_env_var
from .db import (bot_jsonl_path, channel_jsonl_path, connect, now_ts,
                 trusted_user_ids, all_known_chat_ids, DATA_DIR, MEDIA_DIR)
from .listener import make_bot, poll, record_outbound

log = logging.getLogger("tg-local-mcp")

# Module-level handle to the single Bot, set during lifespan startup. The poller
# task and the send tools share it. None until the lifespan runs (e.g. in tests
# that import tools directly without starting the server).
_bot = None
_poll_task: Optional[asyncio.Task] = None
_config = load_config()

# Active self-refreshing typing tasks: {(chat_id, message_thread_id): asyncio.Task}.
# Keyed so start_typing restarts cleanly and send_message cancels automatically.
_typing_tasks: dict = {}


async def _typing_loop(bot, chat_id: int, thread_id: Optional[int],
                       max_seconds: int) -> None:
    """Re-send Telegram typing action every 5s for up to max_seconds."""
    elapsed = 0
    try:
        while elapsed < max_seconds:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(
                    chat_id=chat_id, action="typing",
                    message_thread_id=thread_id,
                )
            await asyncio.sleep(5)
            elapsed += 5
    except asyncio.CancelledError:
        pass


def _cancel_typing(chat_id: int, thread_id: Optional[int]) -> None:
    """Cancel any active typing loop for this (chat_id, thread_id) pair."""
    key = (chat_id, thread_id)
    task = _typing_tasks.get(key)
    if task and not task.done():
        task.cancel()
    _typing_tasks.pop(key, None)


def _default_target() -> Optional[int]:
    """First configured group chat_id, or None if none configured yet."""
    return default_chat_id(_config)


def _bot_username() -> Optional[str]:
    """The bot's configured @username (without @), or None if unset."""
    raw = (_config.get("bot_username") or "").strip()
    return raw.lstrip("@") or None


def _require_bot() -> "Bot":  # type: ignore[name-defined]  # noqa: F821
    if _bot is None:
        var = token_env_var(_config)
        raise RuntimeError(
            f"Bot not initialised — {var} is unset. Set it and restart your Claude "
            "session so the MCP can own the token."
        )
    return _bot


def _resolve_target(chat_id: Optional[int]) -> int:
    target = chat_id if chat_id is not None else _default_target()
    if target is None:
        raise RuntimeError(
            "No chat_id given and no group_chat_ids configured. Add the chat_id of "
            "the group you were added to into config.local.json (group_chat_ids), or "
            "pass chat_id explicitly."
        )
    return target


@contextlib.asynccontextmanager
async def lifespan(server: "FastMCP") -> AsyncIterator[dict]:
    """Start the background poller on startup; cancel it on shutdown."""
    global _bot, _poll_task
    token = resolve_token(_config)
    var = token_env_var(_config)
    if not token:
        log.warning("%s unset and no token file; serving tools but NOT polling "
                    "Telegram. Set %s and restart.", var, var)
        yield {}
        return
    _bot = make_bot(token)
    _poll_task = asyncio.create_task(poll(_bot))
    log.info("poller started for groups=%s", _config.get("group_chat_ids"))
    try:
        yield {}
    finally:
        if _poll_task:
            _poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _poll_task
        if _bot:
            with contextlib.suppress(Exception):
                await _bot.session.close()


mcp = FastMCP(_config.get("mcp_name") or "tg-local-client", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

@mcp.tool()
async def send_message(
    text: str,
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    parse_mode: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
) -> dict:
    """Send a message into your group as this bot.

    text: message body (Telegram limit 4096 chars).
    chat_id: defaults to the first group in config (group_chat_ids[0]). Pass
        another chat_id only if you know what you're doing.
    message_thread_id: for a forum-topic thread within the group, if used.
    parse_mode: 'HTML', 'MarkdownV2', or 'Markdown' (legacy). Default plain text.
    reply_to_message_id: telegram_msg_id of a specific message to reply to, so the
        send shows as a threaded reply in the Telegram client. Pass the inbound
        message's telegram_msg_id to reply to it directly. None = ordinary send.

    Returns the sent message's telegram_msg_id and local DB row id.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    _cancel_typing(target, message_thread_id)
    sent = await bot.send_message(
        chat_id=target,
        text=text,
        message_thread_id=message_thread_id,
        parse_mode=parse_mode,
        reply_to_message_id=reply_to_message_id,
    )
    return record_outbound(sent.message_id, target, text)


@mcp.tool()
async def stream_message_draft(
    text: str,
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    message_id: Optional[int] = None,
    parse_mode: Optional[str] = None,
) -> dict:
    """Send-or-edit a progressive draft message — the thinking→edit pattern as one tool.

    First call (message_id=None): sends a new message and returns its telegram_msg_id.
    Subsequent calls (message_id=<id>): edits that message in place with the new text.

    Typical usage:
      1. draft = stream_message_draft("💭 thinking…", chat_id=X)   # sends
      2. stream_message_draft("Here is my answer…", chat_id=X, message_id=draft["telegram_msg_id"])
      3. (repeat step 2 as content grows)

    Compared to separate send_message + edit_message calls, this keeps the
    message_id threaded through a single tool and auto-stops any active typing
    indicator on the first send.

    text: message body (Telegram limit 4096 chars).
    chat_id: defaults to the first configured group.
    message_thread_id: for forum-topic threads.
    message_id: telegram_msg_id of an existing draft to update. None = send new.
    parse_mode: 'HTML', 'MarkdownV2', or 'Markdown'. Default plain text.

    Returns {telegram_msg_id, chat_id, is_new}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    if message_id is None:
        _cancel_typing(target, message_thread_id)
        sent = await bot.send_message(
            chat_id=target,
            text=text,
            message_thread_id=message_thread_id,
            parse_mode=parse_mode,
        )
        record_outbound(sent.message_id, target, text)
        return {"telegram_msg_id": sent.message_id, "chat_id": target, "is_new": True}
    else:
        await bot.edit_message_text(
            chat_id=target,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
        )
        return {"telegram_msg_id": message_id, "chat_id": target, "is_new": False}


@mcp.tool()
async def send_typing(
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
) -> dict:
    """Send a "typing…" chat action to a chat.

    Telegram shows the indicator in the client for ~5 seconds (Telegram-controlled).
    One call = one 5-second burst. Call again for longer indicators.

    chat_id: defaults to the first configured group.
    message_thread_id: for forum supergroup topics, routes the indicator to that thread.

    Returns {chat_id, ok: True}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    await bot.send_chat_action(
        chat_id=target,
        action="typing",
        message_thread_id=message_thread_id,
    )
    return {"chat_id": target, "ok": True}


@mcp.tool()
async def start_typing(
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    duration_seconds: int = 300,
) -> dict:
    """Start a self-refreshing typing indicator that persists until you send a message.

    Sends the Telegram "typing…" action every ~5 seconds for up to duration_seconds
    (default 300 = 5 minutes). Automatically stops when send_message is called for
    the same chat. Call stop_typing to cancel early.

    Use this at the start of any response that will take more than ~5 seconds —
    call it before doing work, then send_message when done. The indicator stops on
    its own when send_message fires.

    chat_id: defaults to the first configured group.
    message_thread_id: routes the indicator to a forum topic thread.
    duration_seconds: hard cap (default 300s / 5 min).

    Returns {chat_id, ok: True}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    _cancel_typing(target, message_thread_id)
    task = asyncio.create_task(
        _typing_loop(bot, target, message_thread_id, duration_seconds)
    )
    _typing_tasks[(target, message_thread_id)] = task
    return {"chat_id": target, "ok": True}


@mcp.tool()
async def stop_typing(
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
) -> dict:
    """Stop a self-refreshing typing indicator started with start_typing.

    Normally not needed — send_message cancels automatically. Use this if you
    decide not to send a message after all (e.g. on error or standing down).

    Returns {chat_id, ok: True, was_active: bool}.
    """
    target = _resolve_target(chat_id)
    key = (target, message_thread_id)
    was_active = key in _typing_tasks and not _typing_tasks[key].done()
    _cancel_typing(target, message_thread_id)
    return {"chat_id": target, "ok": True, "was_active": was_active}


@mcp.tool()
async def react_to_message(
    telegram_msg_id: int,
    emoji: Optional[str] = None,
    chat_id: Optional[int] = None,
    clear_after_seconds: Optional[float] = None,
) -> dict:
    """Set or clear a reaction emoji on a Telegram message.

    chat_id: defaults to the first configured group.
    telegram_msg_id: the telegram message id to react to.
    emoji: the literal emoji character to react with (e.g. "👀", "✅", "❤").
        Pass None or "" to clear all reactions on the message.
        The Bot API only accepts a curated allowlist — unsupported emojis will error.
    clear_after_seconds: if set, schedule a background task to clear the reaction
        after this many seconds. Fire-and-forget; does not block the return.
        If the MCP server stops before the timer fires, the reaction stays.

    Returns {chat_id, telegram_msg_id, emoji, scheduled_clear_in}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
    await bot.set_message_reaction(
        chat_id=target,
        message_id=telegram_msg_id,
        reaction=reaction,
    )

    if clear_after_seconds and emoji:
        async def _clear_later() -> None:
            await asyncio.sleep(clear_after_seconds)
            try:
                b = _require_bot()
                await b.set_message_reaction(
                    chat_id=target,
                    message_id=telegram_msg_id,
                    reaction=[],
                )
            except Exception:
                pass

        asyncio.create_task(_clear_later())

    return {
        "chat_id": target,
        "telegram_msg_id": telegram_msg_id,
        "emoji": emoji,
        "scheduled_clear_in": clear_after_seconds,
    }


@mcp.tool()
async def edit_message(
    telegram_msg_id: int,
    text: str,
    chat_id: Optional[int] = None,
    parse_mode: Optional[str] = None,
) -> dict:
    """Edit the text of a message this bot previously sent.

    chat_id: defaults to the first configured group.
    telegram_msg_id: the telegram message id to edit.
    text: the new message body. Telegram has a 4096-char limit.
    parse_mode: 'HTML', 'MarkdownV2', or 'Markdown' (legacy). Default plain text.

    Only works on messages this bot sent (Telegram disallows editing others').
    Returns {telegram_msg_id, chat_id, ok}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    await bot.edit_message_text(
        chat_id=target,
        message_id=telegram_msg_id,
        text=text,
        parse_mode=parse_mode,
    )
    # Best-effort: reflect the new text on the stored outbound row.
    try:
        conn = connect()
        try:
            conn.execute(
                "UPDATE messages SET text = ? "
                "WHERE telegram_msg_id = ? AND chat_id = ? AND direction = 'out'",
                (text, telegram_msg_id, target),
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return {"telegram_msg_id": telegram_msg_id, "chat_id": target, "ok": True}


@mcp.tool()
async def delete_message(
    telegram_msg_id: int,
    chat_id: Optional[int] = None,
) -> dict:
    """Delete a message this bot previously sent.

    chat_id: defaults to the first configured group.
    telegram_msg_id: the telegram message id to delete.

    A bot can delete its own messages anytime; deleting others' requires admin
    rights and is subject to Telegram's 48-hour limit in groups.
    Returns {telegram_msg_id, chat_id, ok}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    await bot.delete_message(chat_id=target, message_id=telegram_msg_id)
    # Best-effort: drop the stored outbound row.
    try:
        conn = connect()
        try:
            conn.execute(
                "DELETE FROM messages "
                "WHERE telegram_msg_id = ? AND chat_id = ? AND direction = 'out'",
                (telegram_msg_id, target),
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return {"telegram_msg_id": telegram_msg_id, "chat_id": target, "ok": True}


@mcp.tool()
async def send_photo(
    path: str,
    caption: Optional[str] = None,
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    parse_mode: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
) -> dict:
    """Send a photo from the local filesystem. Telegram compresses/resizes it.

    Use send_document for files that must stay unmodified.

    path: absolute path to the image file on disk.
    caption: optional caption text (max 1024 chars).
    chat_id: defaults to the first configured group.
    message_thread_id: forum topic thread, if used.
    parse_mode: 'HTML', 'MarkdownV2', or 'Markdown' (legacy). Default plain text.
    reply_to_message_id: telegram_msg_id to reply to.

    Returns {telegram_msg_id, db_id, chat_id, kind, path}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    file = FSInputFile(path)
    sent = await bot.send_photo(
        chat_id=target,
        photo=file,
        caption=caption,
        parse_mode=parse_mode,
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
    )
    summary = f"[photo: {os.path.basename(path)}]" + (f" {caption}" if caption else "")
    out = record_outbound(sent.message_id, target, summary)
    out.update({"kind": "photo", "path": path})
    return out


@mcp.tool()
async def send_document(
    path: str,
    caption: Optional[str] = None,
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    parse_mode: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
) -> dict:
    """Send any file as a document (no compression). Up to 50MB via Bot API.

    path: absolute path to the file on disk.
    caption: optional caption text (max 1024 chars).
    chat_id: defaults to the first configured group.
    message_thread_id: forum topic thread, if used.
    parse_mode: 'HTML', 'MarkdownV2', or 'Markdown' (legacy). Default plain text.
    reply_to_message_id: telegram_msg_id to reply to.

    Returns {telegram_msg_id, db_id, chat_id, kind, path}.
    """
    bot = _require_bot()
    target = _resolve_target(chat_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    file = FSInputFile(path)
    sent = await bot.send_document(
        chat_id=target,
        document=file,
        caption=caption,
        parse_mode=parse_mode,
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
    )
    summary = f"[document: {os.path.basename(path)}]" + (f" {caption}" if caption else "")
    out = record_outbound(sent.message_id, target, summary)
    out.update({"kind": "document", "path": path})
    return out


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------

@mcp.tool()
def list_recent_messages(limit: int = 20, unread_only: bool = False,
                         chat_id: Optional[int] = None,
                         since_id: Optional[int] = None) -> list[dict]:
    """List recent inbound messages from the local store, newest-first.

    limit: max rows. unread_only: only messages with read_at IS NULL.
    chat_id: filter to a chat (defaults to all chats this client has seen).
    since_id: only return messages with local DB id > since_id. Use the
        highest id from the previous session's catch-up to avoid re-processing
        already-handled messages on restart (idea-162).

    Each row includes reply_to_telegram_msg_id (non-null when the message is a
    threaded reply) and quote_text (non-null when the sender quote-replied,
    highlighting a specific snippet) — both surfacing thread/quote context for the
    monitoring agent. quote_is_manual is 1 for a hand-selected quote.
    """
    where = ["direction = 'in'"]
    params: list = []
    if unread_only:
        where.append("read_at IS NULL")
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    if since_id is not None:
        where.append("id > ?")
        params.append(since_id)
    params.append(limit)
    conn = connect()
    try:
        # Dedup at read time: for each (telegram_msg_id, chat_id) keep the latest row
        # (highest id). Edits are stored as new rows; this surfaces only the current
        # version. COALESCE handles the rare case of a NULL telegram_msg_id.
        rows = conn.execute(
            f"""WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(telegram_msg_id, -id), chat_id
                        ORDER BY id DESC
                    ) AS _rn
                    FROM messages WHERE {" AND ".join(where)}
                )
                SELECT id, bot_slug, telegram_msg_id, chat_id, from_user_id, from_username,
                       from_first_name, text, ts, read_at, message_thread_id,
                       reply_to_telegram_msg_id, quote_text, quote_is_manual,
                       media_type, media_file_id, media_file_size, media_mime_type
                FROM ranked WHERE _rn = 1
                ORDER BY ts DESC LIMIT ?""",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def list_known_chats() -> list[dict]:
    """List all chat_ids the client has seen, with last-message metadata + names.

    Each row carries the chat's human-readable `title` and `type` (from the chats
    table, LEFT JOINed on chat_id). `title`/`type` may be null for chats first seen
    before name capture landed — they self-populate as new traffic flows through.

    Returns rows newest-first, each: {chat_id, title, type, last_ts,
    inbound_count, outbound_count, last_inbound_username}.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT m1.chat_id AS chat_id,
                      c.title AS title,
                      c.type AS type,
                      MAX(m1.ts) AS last_ts,
                      COUNT(*) FILTER (WHERE m1.direction='in') AS inbound_count,
                      COUNT(*) FILTER (WHERE m1.direction='out') AS outbound_count,
                      (SELECT from_username FROM messages m2
                       WHERE m2.chat_id = m1.chat_id AND direction='in'
                       ORDER BY ts DESC LIMIT 1) AS last_inbound_username
               FROM messages m1
               LEFT JOIN chats c ON c.chat_id = m1.chat_id
               GROUP BY m1.chat_id
               ORDER BY last_ts DESC""",
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def mark_read(message_ids: list[int]) -> dict:
    """Mark inbound messages as read by local DB id."""
    if not message_ids:
        return {"updated": 0}
    placeholders = ",".join("?" * len(message_ids))
    conn = connect()
    try:
        cur = conn.execute(
            f"UPDATE messages SET read_at = ? WHERE id IN ({placeholders}) "
            "AND direction = 'in' AND read_at IS NULL",
            (now_ts(), *message_ids),
        )
        return {"updated": cur.rowcount}
    finally:
        conn.close()


@mcp.tool()
async def download_media(message_id: int, dest_dir: Optional[str] = None) -> dict:
    """Download a media attachment from a stored inbound message.

    message_id: local DB id (the 'id' field in list_recent_messages).
    dest_dir: directory to save into. Defaults to the client media dir.

    Returns {path, media_type, size_bytes, cached}. Path is written even if the
    file was already cached from a previous call.
    """
    bot = _require_bot()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ? AND direction = 'in'",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"no inbound message with id={message_id}")
    row = dict(row)
    if not row.get("media_file_id"):
        raise ValueError(f"message {message_id} has no media")
    if row.get("media_local_path") and os.path.isfile(row["media_local_path"]):
        return {"path": row["media_local_path"], "media_type": row["media_type"],
                "size_bytes": os.path.getsize(row["media_local_path"]), "cached": True}

    dest = Path(dest_dir) if dest_dir else MEDIA_DIR
    dest.mkdir(parents=True, exist_ok=True)

    # Resolve the file_path and download it.
    tg_file = await bot.get_file(row["media_file_id"])
    file_path = tg_file.file_path or ""
    ext = ""
    if "." in file_path:
        ext = "." + file_path.rsplit(".", 1)[1]
    # media_file_unique_id may be absent on older rows; fall back to file_id hash.
    unique_id = row.get("media_file_unique_id") or row["media_file_id"][:20]
    out = dest / f"{row['media_type']}_{unique_id}{ext}"
    await bot.download_file(tg_file.file_path, destination=str(out))

    conn = connect()
    try:
        conn.execute("UPDATE messages SET media_local_path = ? WHERE id = ?",
                     (str(out), message_id))
    finally:
        conn.close()
    return {"path": str(out), "media_type": row["media_type"],
            "size_bytes": os.path.getsize(out), "cached": False}


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

@mcp.tool()
def get_tail_command(
    chat_id: Optional[int] = None,
    from_username: Optional[str] = None,
    message_thread_id: Optional[int] = None,
    channels: Optional[list[dict]] = None,
    wake_on: Optional[list[str]] = None,
    trusted_identities: Optional[list[str]] = None,
    triage: Optional[dict] = None,
    cursor: bool = True,
) -> dict:
    """Return a FLAT command that monitors channel JSONL file(s) for new messages.

    The returned `command` is a single `uv run` invocation of the `tg-local-tail`
    entry point — NO shell pipe, NO jq, NO shell operators. That lets Claude Code
    allowlist it with one prefix so the Bash permission prompt never fires when you
    set up your monitor. Run it with Bash run_in_background, then Monitor the
    process id to be notified of new messages live.

    ## Routing

    Always tails the single per-bot file (channels/<bot_slug>.jsonl) — one file
    holds everything the bot sees: all DMs and every group it's in, in arrival
    order. This matches the fabric's per-bot design.

    `channels` and `chat_id` are retained as in-tail topic/thread filter hints
    (via --channel-topics), not file selectors.

    ## Fine filtering (deterministic, all opt-in)

    `from_username`: only emit records from this sender.
    `message_thread_id`: only emit records in this forum-topic thread.

    ## Per-channel topic scope (`channels`)

    A list of {chat_id, topics} dicts giving each channel its OWN topic scope:
      - "all" (or omitted)  → all topics in that channel (default)
      - "general"           → general topic only (message_thread_id is null)
      - [7, 9] (list of int)→ only those specific forum-topic thread ids

    ## Sender allow-list (`wake_on`)

    A list naming which cheap relevance clauses to require; a record passes if
    AT LEAST ONE is satisfied:
      - "mention"        → text @-mentions this bot's @username
                           (resolved from config bot_username).
      - "trusted_humans" → the sender's from_user_id is a trusted human.
                           Resolved from the trusted_identities table using
                           `trusted_identities` (list of labels like ["james"]);
                           if omitted, ALL trusted identities are used.

    ## Haiku ACT/SKIP triage (opt-in, `triage`)

    Pass `triage` to bundle a SECOND layer: for each line surviving deterministic
    filters, a cheap `claude -p --model haiku` decides ACT or SKIP, and the line
    reaches stdout ONLY if ACT.

    `triage` dict:
      - `role`: short agent identity/responsibility string.
      - `state_file`: path to a file the agent keeps current with its waiting-on
        state; read FRESH per message.
      - `model`: claude model alias (default "haiku").

    The Haiku call runs in a neutral cwd (/tmp) with MCP disabled so it never
    loads this project's CLAUDE.md / MCP / skills. The classifier is FAIL-SAFE:
    any error, timeout, or malformed Haiku output → ACT.

    Returns {command, jsonl_path(s), wake_filter, triage_filter}.
    """
    # Parse per-channel topic spec.
    channel_topic_tokens: dict[int, str] = {}
    channel_ids_from_spec: list[int] = []
    for entry in channels or []:
        cid = entry.get("chat_id")
        if cid is None:
            continue
        cid = int(cid)
        channel_ids_from_spec.append(cid)
        topics = entry.get("topics", "all")
        if topics is None or topics == "all":
            token = "all"
        elif topics == "general":
            token = "general"
        elif isinstance(topics, (list, tuple)):
            token = ",".join(str(int(t)) for t in topics)
        else:
            token = str(topics)
        channel_topic_tokens[cid] = token

    # Always tail the single per-bot file — one file holds everything the bot
    # sees (all DMs + all groups), matching the fabric's per-bot design.
    # chat_id / channels args still control in-tail topic filters below.
    files = [bot_jsonl_path()]

    file_paths = [str(f) for f in files]
    args = list(file_paths)

    if from_username:
        safe = "".join(c for c in from_username if c.isalnum() or c == "_")
        args += ["--from-username", safe]
    if message_thread_id is not None:
        args += ["--message-thread-id", str(message_thread_id)]

    # Per-channel topic specs.
    for cid in channel_ids_from_spec:
        args += [f"--channel-topics={cid}:{channel_topic_tokens[cid]}"]

    # Sender / @-mention relevance pre-filter (opt-in).
    wake_filter: Optional[dict] = None
    if wake_on:
        resolved_mention: Optional[str] = None
        resolved_trusted: list[int] = []
        for clause in wake_on:
            args += ["--wake-on", clause]
        if "mention" in wake_on:
            uname = _bot_username()
            if uname:
                safe_u = "".join(c for c in uname if c.isalnum() or c == "_")
                if safe_u:
                    resolved_mention = safe_u
                    args += ["--mention-username", safe_u]
        if "trusted_humans" in wake_on:
            resolved_trusted = trusted_user_ids(trusted_identities)
            for uid in resolved_trusted:
                args += ["--trusted-user-id", str(uid)]
        wake_filter = {
            "wake_on": list(wake_on),
            "mention_username": resolved_mention,
            "trusted_user_ids": resolved_trusted,
        }

    # Haiku triage layer (opt-in).
    triage_filter: Optional[dict] = None
    if triage:
        raw_role = (triage.get("role") or "").strip()
        role = raw_role
        for op in ("|", "&&", ";", "$("):
            role = role.replace(op, " ")
        role = " ".join(role.split())
        state_file = triage.get("state_file")
        model = triage.get("model") or "haiku"
        args += ["--triage"]
        if role:
            args += ["--triage-role", shlex.quote(role)]
        if state_file:
            args += ["--triage-state-file", shlex.quote(str(state_file))]
        args += ["--triage-model", shlex.quote(str(model))]
        triage_filter = {"role": role or None, "state_file": state_file, "model": model}

    # Durable cursor (default ON). Build a stable per-channel-set cursor path so
    # restarts are gap-free without extra steps from the caller.
    cursor_path: Optional[str] = None
    if cursor:
        import hashlib as _hashlib
        from .db import DATA_DIR as _DATA_DIR
        digest = _hashlib.sha1(
            "\n".join(sorted(file_paths)).encode()
        ).hexdigest()[:12]
        cursor_name = f"tail-cursor-{digest}.json"
        cursor_path = str(_DATA_DIR / cursor_name)
        args += ["--cursor-file", cursor_path]

    client_dir = str(Path(__file__).resolve().parent.parent.parent)
    cmd = f"uv run --directory {client_dir} tg-local-tail " + " ".join(args)
    return {
        "command": cmd,
        "jsonl_paths": file_paths,
        "wake_filter": wake_filter,
        "triage_filter": triage_filter,
        "cursor_file": cursor_path,
    }


# ---------------------------------------------------------------------------
# Identity / trust
# ---------------------------------------------------------------------------

@mcp.tool()
def lookup_identity(user_id: int) -> dict:
    """Look up the logical identity for a Telegram user_id, if trusted.

    Returns {identity, trusted: bool, row}. If untrusted, row is None and
    identity is None. Call this whenever an inbound message arrives from a
    user_id you don't immediately recognise — the answer tells you whether to
    honour it as if it were the canonical user.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM trusted_identities WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return {"identity": row["identity"], "trusted": True, "row": dict(row)}
    return {"identity": None, "trusted": False, "row": None}


@mcp.tool()
def list_trusted_identities(identity: Optional[str] = None) -> list[dict]:
    """List trusted identity bindings. Optionally filter by identity label."""
    conn = connect()
    try:
        if identity:
            rows = conn.execute(
                "SELECT * FROM trusted_identities WHERE identity = ? "
                "ORDER BY trusted_at DESC",
                (identity,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trusted_identities ORDER BY identity, trusted_at DESC"
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def trust_identity(
    user_id: int,
    identity: str,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    trusted_by_user_id: Optional[int] = None,
    note: Optional[str] = None,
) -> dict:
    """Record a durable binding from a Telegram user_id to a logical identity name.

    identity: a short label like "james" — multiple user_ids can map to the same
        identity (e.g. the same person on two devices). Used by trust checks.
    trusted_by_user_id: the user_id who authorised this binding (typically the
        canonical account). Audit trail only.

    Idempotent: re-inserting an existing user_id updates the row.
    """
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO trusted_identities
               (user_id, username, display_name, identity, trusted_at,
                trusted_by_user_id, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 display_name = excluded.display_name,
                 identity = excluded.identity,
                 trusted_at = excluded.trusted_at,
                 trusted_by_user_id = COALESCE(excluded.trusted_by_user_id,
                                               trusted_identities.trusted_by_user_id),
                 note = COALESCE(excluded.note, trusted_identities.note)""",
            (user_id, username, display_name, identity, now_ts(),
             trusted_by_user_id, note),
        )
    finally:
        conn.close()
    return {"user_id": user_id, "identity": identity, "ok": True}


@mcp.tool()
def untrust_identity(user_id: int) -> dict:
    """Remove a trusted-identity binding. Returns {removed: 0|1}."""
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM trusted_identities WHERE user_id = ?", (user_id,)
        )
    finally:
        conn.close()
    return {"removed": cur.rowcount, "user_id": user_id}


# ---------------------------------------------------------------------------
# Forum topics
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_forum_topic(
    chat_id: int,
    name: str,
    icon_color: Optional[int] = None,
    icon_custom_emoji_id: Optional[str] = None,
) -> dict:
    """Create a new topic in a forum-enabled supergroup.

    chat_id: numeric Telegram chat id of the target forum supergroup (negative id
        starting with -100). A plain group or non-forum supergroup will have the
        call rejected by Telegram.
    name: topic name, 1–128 characters.
    icon_color: optional RGB integer from Telegram's palette: 7322096 (blue),
        16766590 (yellow), 13338331 (violet), 9367192 (green), 16749490 (pink),
        16478047 (red).
    icon_custom_emoji_id: optional custom emoji id for the topic icon.

    Returns {message_thread_id, name, chat_id}. Pass the returned
    message_thread_id to send_message(message_thread_id=...) to post into the
    new topic.
    """
    bot = _require_bot()
    topic = await bot.create_forum_topic(
        chat_id=chat_id,
        name=name,
        icon_color=icon_color,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )
    return {
        "message_thread_id": topic.message_thread_id,
        "name": topic.name,
        "chat_id": chat_id,
    }


@mcp.tool()
async def edit_forum_topic(
    chat_id: int,
    message_thread_id: int,
    name: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
) -> dict:
    """Edit the name and/or icon of an existing forum topic.

    chat_id: numeric Telegram chat id of the forum supergroup.
    message_thread_id: the topic's thread id (as returned by create_forum_topic
        or from an inbound forum message).
    name: new topic name (0–128 chars). Omit or pass None to keep the current name.
    icon_custom_emoji_id: new custom emoji for the icon. Pass "" to remove the
        current icon. Omit to keep unchanged.

    Returns {ok, chat_id, message_thread_id}.
    """
    bot = _require_bot()
    ok = await bot.edit_forum_topic(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        name=name,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )
    return {"ok": bool(ok), "chat_id": chat_id, "message_thread_id": message_thread_id}


@mcp.tool()
async def close_forum_topic(
    chat_id: int,
    message_thread_id: int,
) -> dict:
    """Close an open forum topic so no new messages can be sent into it.

    The bot must be an admin with can_manage_topics, or the topic creator.
    Returns {ok, chat_id, message_thread_id}.
    """
    bot = _require_bot()
    ok = await bot.close_forum_topic(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )
    return {"ok": bool(ok), "chat_id": chat_id, "message_thread_id": message_thread_id}


@mcp.tool()
async def reopen_forum_topic(
    chat_id: int,
    message_thread_id: int,
) -> dict:
    """Reopen a previously closed forum topic.

    The bot must be an admin with can_manage_topics, or the topic creator.
    Returns {ok, chat_id, message_thread_id}.
    """
    bot = _require_bot()
    ok = await bot.reopen_forum_topic(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )
    return {"ok": bool(ok), "chat_id": chat_id, "message_thread_id": message_thread_id}


# ---------------------------------------------------------------------------
# Bot info
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_me() -> dict:
    """Return this bot's own profile as reported by Telegram.

    Useful for diagnosing group message visibility issues — the response
    includes can_read_all_group_messages (False = privacy mode on; bot only
    sees messages that @mention it directly) and can_join_groups.

    Returns the full User object Telegram provides for bots: id, is_bot,
    first_name, username, can_join_groups, can_read_all_group_messages,
    supports_inline_queries, and any other fields Telegram includes.
    """
    bot = _require_bot()
    me = await bot.get_me()
    return {
        "id": me.id,
        "is_bot": me.is_bot,
        "first_name": me.first_name,
        "username": me.username,
        "can_join_groups": me.can_join_groups,
        "can_read_all_group_messages": me.can_read_all_group_messages,
        "supports_inline_queries": me.supports_inline_queries,
    }


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@mcp.tool()
async def client_version() -> dict:
    """Report the RUNNING MCP server's loaded version + registered tool set.

    Two complementary signals, both reflecting the code ACTUALLY LOADED in this
    process — never what's on disk:

    - `version`: the CLIENT_VERSION constant compiled into the module at import.
      Bumped on every client change (see _version.py for the bump convention).
    - `tools`: the sorted names of the MCP tools this running server has actually
      registered, introspected live from the FastMCP app's tool registry. This
      auto-reflects exactly which tools the loaded code exposes and needs no
      manual upkeep — it's the most reliable staleness signal.

    Deliberately does NOT consult live git HEAD: the on-disk HEAD can be newer
    than the code this process loaded, which would falsely report "current" for
    a stale process. The whole value here is reporting the loaded code.

    Returns {version, tools, tool_count}.
    """
    registered = await mcp.list_tools()
    names = sorted(t.name for t in registered)
    return {
        "version": CLIENT_VERSION,
        "tools": names,
        "tool_count": len(names),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    run()
