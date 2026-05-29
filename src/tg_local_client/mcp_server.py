"""Single-process MCP + poller for the generic local Telegram client.

ONE process: the stdio MCP server owns the bot token, runs a background asyncio
task that long-polls Telegram (appending inbound messages to per-channel JSONL +
SQLite), and serves a small tool surface (send_message, list_recent_messages,
get_tail_command, mark_read). No control socket, no registry, no fabric internals.

When the agent's Claude session (and thus this MCP) is up, it's listening; when it
closes, polling stops — that's fine. Telegram is the only shared substrate.

Everything bot-specific (server name, which group(s) to watch, which env var holds
the token) comes from config.local.json — see config.py.
"""
import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

from fastmcp import FastMCP

from .config import default_chat_id, load_config, resolve_token, token_env_var
from .db import channel_jsonl_path, connect, now_ts
from .listener import make_bot, poll, record_outbound

log = logging.getLogger("tg-local-mcp")

# Module-level handle to the single Bot, set during lifespan startup. The poller
# task and the send tool share it. None until the lifespan runs (e.g. in tests
# that import tools directly without starting the server).
_bot = None
_poll_task: Optional[asyncio.Task] = None
_config = load_config()


def _default_target() -> Optional[int]:
    """First configured group chat_id, or None if none configured yet."""
    return default_chat_id(_config)


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


@mcp.tool()
async def send_message(
    text: str,
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
) -> dict:
    """Send a message into your group as this bot.

    text: message body (Telegram limit 4096 chars). Plain text.
    chat_id: defaults to the first group in config (group_chat_ids[0]). Pass another
        chat_id only if you know what you're doing — this bot is scoped to its
        configured group(s).
    message_thread_id: for a forum-topic thread within the group, if used.

    Returns the sent message's telegram_msg_id and local DB row id.
    """
    if _bot is None:
        var = token_env_var(_config)
        raise RuntimeError(
            f"Bot not initialised — {var} is unset. Set it and restart your Claude "
            "session so the MCP can own the token."
        )
    target = chat_id if chat_id is not None else _default_target()
    if target is None:
        raise RuntimeError(
            "No chat_id given and no group_chat_ids configured. Add the chat_id of "
            "the group you were added to into config.local.json (group_chat_ids), or "
            "pass chat_id explicitly."
        )
    sent = await _bot.send_message(
        chat_id=target, text=text, message_thread_id=message_thread_id,
    )
    return record_outbound(sent.message_id, target, text)


@mcp.tool()
def list_recent_messages(limit: int = 20, unread_only: bool = False,
                         chat_id: Optional[int] = None) -> list[dict]:
    """List recent inbound messages from the local store, newest-first.

    limit: max rows. unread_only: only messages with read_at IS NULL.
    chat_id: filter to a chat (defaults to all chats this client has seen).
    """
    where = ["direction = 'in'"]
    params: list = []
    if unread_only:
        where.append("read_at IS NULL")
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    params.append(limit)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT id, telegram_msg_id, chat_id, from_user_id, from_username,
                       from_first_name, text, ts, read_at, message_thread_id,
                       media_type, media_file_id, media_file_size, media_mime_type
                FROM messages WHERE {" AND ".join(where)}
                ORDER BY ts DESC LIMIT ?""",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def get_tail_command(chat_id: Optional[int] = None,
                     from_username: Optional[str] = None,
                     message_thread_id: Optional[int] = None) -> dict:
    """Return a FLAT command that monitors the channel JSONL file for new messages.

    The returned `command` is a single `uv run` invocation of the `tg-local-tail`
    entry point — NO shell pipe, NO jq, NO shell operators. That lets Claude Code
    allowlist it with one prefix so the Bash permission prompt never fires when you
    set up your monitor. Run it with Bash run_in_background, then Monitor the process
    id to be notified of new messages live.

    chat_id: which channel to follow (defaults to the first configured group).
    from_username / message_thread_id: optional fine-filters applied in-process.

    NOTE: ignore messages from your OWN bot's from_user_id — Telegram echoes a bot's
    own sends back through getUpdates, so a monitor without that filter sees both
    inbound traffic and your own outbound. Filter them out client-side.

    Returns {command, jsonl_path}.
    """
    target = chat_id if chat_id is not None else _default_target()
    if target is None:
        raise RuntimeError(
            "No chat_id given and no group_chat_ids configured. Add the chat_id of "
            "your group into config.local.json (group_chat_ids), or pass chat_id."
        )
    jsonl_path = str(channel_jsonl_path(target))
    args = [jsonl_path]
    if from_username:
        safe = "".join(c for c in from_username if c.isalnum() or c == "_")
        args += ["--from-username", safe]
    if message_thread_id is not None:
        args += ["--message-thread-id", str(message_thread_id)]
    client_dir = str(Path(__file__).resolve().parent.parent.parent)
    cmd = f"uv run --directory {client_dir} tg-local-tail " + " ".join(args)
    return {"command": cmd, "jsonl_path": jsonl_path}


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


def run() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    run()
