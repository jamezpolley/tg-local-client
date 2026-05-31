"""MCP tools: wiring, payloads, guard rails, and new tool coverage."""
import asyncio
import json
import os

import pytest

import tg_local_client.db as db
import tg_local_client.mcp_server as mcp_server


def _unwrap(tool):
    """FastMCP wraps functions in a Tool/FunctionTool; get the underlying callable."""
    return getattr(tool, "fn", tool)


def _reload_data_dir(tmp_path, monkeypatch, bot_slug="test"):
    """Point the db module at a temp data dir so tests never touch the real DB."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "messages.db")
    monkeypatch.setattr(db, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(db, "BOT_SLUG", bot_slug)


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

class _FakeBot:
    """Minimal Bot stand-in that accepts the full keyword surface send_message uses."""

    def __init__(self):
        self.sent = {}

    async def send_message(self, chat_id, text, message_thread_id=None,
                           parse_mode=None, reply_to_message_id=None):
        self.sent.update(chat_id=chat_id, text=text,
                         message_thread_id=message_thread_id,
                         parse_mode=parse_mode,
                         reply_to_message_id=reply_to_message_id)

        class Sent:
            message_id = 555
        return Sent()

    async def send_chat_action(self, chat_id, action, message_thread_id=None):
        self.sent.update(chat_id=chat_id, action=action)

    async def set_message_reaction(self, chat_id, message_id, reaction):
        self.sent.update(reaction_chat=chat_id, reaction_msg=message_id,
                         reaction=reaction)

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        self.sent.update(edited_chat=chat_id, edited_msg=message_id, edited_text=text)

    async def delete_message(self, chat_id, message_id):
        self.sent.update(deleted_chat=chat_id, deleted_msg=message_id)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None,
                         message_thread_id=None, reply_to_message_id=None):
        self.sent.update(photo_chat=chat_id, photo=photo, caption=caption)

        class Sent:
            message_id = 777
        return Sent()

    async def send_document(self, chat_id, document, caption=None, parse_mode=None,
                            message_thread_id=None, reply_to_message_id=None):
        self.sent.update(doc_chat=chat_id, doc=document, caption=caption)

        class Sent:
            message_id = 888
        return Sent()


def test_send_message_builds_correct_payload(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    monkeypatch.setattr(mcp_server, "record_outbound",
                        lambda mid, cid, txt: {"telegram_msg_id": mid, "chat_id": cid})

    fn = _unwrap(mcp_server.send_message)
    result = asyncio.run(fn(text="ack: on it"))
    # Defaulted to the first configured group.
    assert fake.sent["chat_id"] == -100999
    assert fake.sent["text"] == "ack: on it"
    assert result["telegram_msg_id"] == 555


def test_send_message_with_parse_mode_and_reply(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    monkeypatch.setattr(mcp_server, "record_outbound",
                        lambda mid, cid, txt: {"telegram_msg_id": mid, "chat_id": cid})

    fn = _unwrap(mcp_server.send_message)
    asyncio.run(fn(text="<b>html</b>", parse_mode="HTML", reply_to_message_id=42))
    assert fake.sent["parse_mode"] == "HTML"
    assert fake.sent["reply_to_message_id"] == 42


def test_send_message_raises_when_no_group_configured(monkeypatch):
    class FakeBot:
        async def send_message(self, **kw):  # pragma: no cover
            raise AssertionError("should not be called")

    monkeypatch.setattr(mcp_server, "_bot", FakeBot())
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.send_message)
    with pytest.raises(RuntimeError, match="group_chat_ids"):
        asyncio.run(fn(text="nowhere to go"))


# ---------------------------------------------------------------------------
# send_typing
# ---------------------------------------------------------------------------

def test_send_typing(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.send_typing)
    result = asyncio.run(fn())
    assert fake.sent["action"] == "typing"
    assert result["ok"] is True
    assert result["chat_id"] == -100999


def test_send_typing_no_bot_raises(monkeypatch):
    monkeypatch.setattr(mcp_server, "_bot", None)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.send_typing)
    with pytest.raises(RuntimeError, match="TG_BOT_TOKEN"):
        asyncio.run(fn())


# ---------------------------------------------------------------------------
# react_to_message
# ---------------------------------------------------------------------------

def test_react_to_message(monkeypatch):
    from aiogram.types import ReactionTypeEmoji
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.react_to_message)
    result = asyncio.run(fn(telegram_msg_id=10, emoji="👀"))
    assert fake.sent["reaction_msg"] == 10
    assert len(fake.sent["reaction"]) == 1
    assert result["emoji"] == "👀"


def test_react_to_message_clear(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.react_to_message)
    result = asyncio.run(fn(telegram_msg_id=10, emoji=None))
    assert fake.sent["reaction"] == []
    assert result["emoji"] is None


# ---------------------------------------------------------------------------
# edit_message
# ---------------------------------------------------------------------------

def test_edit_message(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    # Seed an outbound row first.
    conn = db.connect()
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (101, -100999, 'old text', 1, 'out')")
    conn.close()

    fn = _unwrap(mcp_server.edit_message)
    result = asyncio.run(fn(telegram_msg_id=101, text="new text"))
    assert fake.sent["edited_msg"] == 101
    assert fake.sent["edited_text"] == "new text"
    assert result["ok"] is True


def test_edit_message_error_path(monkeypatch):
    """edit_message raises the Telegram error to the caller."""
    class ErrorBot:
        async def edit_message_text(self, **kw):
            raise RuntimeError("Message can't be edited")

    monkeypatch.setattr(mcp_server, "_bot", ErrorBot())
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.edit_message)
    with pytest.raises(RuntimeError, match="edited"):
        asyncio.run(fn(telegram_msg_id=5, text="nope"))


# ---------------------------------------------------------------------------
# delete_message
# ---------------------------------------------------------------------------

def test_delete_message(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.delete_message)
    result = asyncio.run(fn(telegram_msg_id=200))
    assert fake.sent["deleted_msg"] == 200
    assert result["ok"] is True


def test_delete_message_error_path(monkeypatch):
    class ErrorBot:
        async def delete_message(self, **kw):
            raise RuntimeError("MESSAGE_DELETE_FORBIDDEN")

    monkeypatch.setattr(mcp_server, "_bot", ErrorBot())
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.delete_message)
    with pytest.raises(RuntimeError, match="FORBIDDEN"):
        asyncio.run(fn(telegram_msg_id=5))


# ---------------------------------------------------------------------------
# send_photo / send_document
# ---------------------------------------------------------------------------

def test_send_photo(monkeypatch, tmp_path):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    monkeypatch.setattr(mcp_server, "record_outbound",
                        lambda mid, cid, txt: {"telegram_msg_id": mid, "chat_id": cid})
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n")

    fn = _unwrap(mcp_server.send_photo)
    result = asyncio.run(fn(path=str(img), caption="look at this"))
    assert fake.sent["photo_chat"] == -100999
    assert fake.sent["caption"] == "look at this"
    assert result["kind"] == "photo"


def test_send_photo_missing_file(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.send_photo)
    with pytest.raises(FileNotFoundError):
        asyncio.run(fn(path="/nonexistent/file.png"))


def test_send_document(monkeypatch, tmp_path):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    monkeypatch.setattr(mcp_server, "record_outbound",
                        lambda mid, cid, txt: {"telegram_msg_id": mid, "chat_id": cid})
    doc = tmp_path / "data.csv"
    doc.write_text("a,b,c\n")

    fn = _unwrap(mcp_server.send_document)
    result = asyncio.run(fn(path=str(doc)))
    assert fake.sent["doc_chat"] == -100999
    assert result["kind"] == "document"


def test_send_document_missing_file(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(mcp_server, "_bot", fake)
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.send_document)
    with pytest.raises(FileNotFoundError):
        asyncio.run(fn(path="/nonexistent/file.pdf"))


# ---------------------------------------------------------------------------
# list_known_chats
# ---------------------------------------------------------------------------

def test_list_known_chats_returns_rows(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (1, -10001, 'hi', 1000, 'in')")
    conn.execute("INSERT INTO chats (chat_id, title, type, first_seen_ts, last_seen_ts) "
                 "VALUES (-10001, 'Test Group', 'supergroup', 1000, 1000)")
    conn.close()

    fn = _unwrap(mcp_server.list_known_chats)
    rows = fn()
    assert len(rows) == 1
    assert rows[0]["chat_id"] == -10001
    assert rows[0]["title"] == "Test Group"
    assert rows[0]["inbound_count"] == 1


# ---------------------------------------------------------------------------
# get_tail_command
# ---------------------------------------------------------------------------

def test_get_tail_command_is_flat_and_pipe_free(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="mybot")
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn()
    cmd = out["command"]
    for forbidden in ("|", "&&", ";", "$(", "jq", ">", "<"):
        assert forbidden not in cmd, f"command contains forbidden token {forbidden!r}: {cmd}"
    assert cmd.startswith("uv run --directory ")
    assert "tg-local-tail" in cmd
    # Always returns a single per-bot file.
    assert len(out["jsonl_paths"]) == 1
    assert "mybot.jsonl" in out["jsonl_paths"][0]


def test_get_tail_command_filter_sanitised(monkeypatch):
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn(from_username="bad; rm -rf /")
    assert "rm" in out["command"]   # letters survive
    assert ";" not in out["command"]


def test_get_tail_command_always_returns_per_bot_file(monkeypatch, tmp_path):
    """get_tail_command always returns the single per-bot file, even on fresh install."""
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="mybot")
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": []})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn()
    assert len(out["jsonl_paths"]) == 1
    assert "mybot.jsonl" in out["jsonl_paths"][0]


def test_get_tail_command_wake_on_mention(monkeypatch):
    monkeypatch.setattr(mcp_server, "_config", {
        "group_chat_ids": [-100999],
        "bot_username": "mybot",
    })
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn(wake_on=["mention"])
    cmd = out["command"]
    assert "--wake-on" in cmd
    assert "--mention-username" in cmd
    assert "mybot" in cmd
    assert out["wake_filter"]["mention_username"] == "mybot"


def test_get_tail_command_triage_flags(monkeypatch):
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn(triage={"role": "the upload agent", "model": "haiku"})
    cmd = out["command"]
    assert "--triage" in cmd
    assert "--triage-role" in cmd
    assert "--triage-model" in cmd
    assert out["triage_filter"]["model"] == "haiku"


def test_get_tail_command_triage_strips_shell_ops(monkeypatch):
    """Shell operators in the role string must be stripped so command stays flat."""
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn(triage={"role": "agent | rm -rf / && bad"})
    # Operators stripped from role in triage_filter.
    role = out["triage_filter"]["role"]
    assert "|" not in role
    assert "&&" not in role


def test_get_tail_command_channel_topics(monkeypatch, tmp_path):
    """Per-channel topic specs produce --channel-topics= flags; still one per-bot file."""
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="mybot")
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn(channels=[{"chat_id": -100999, "topics": "general"},
                        {"chat_id": -10007, "topics": [5, 9]}])
    cmd = out["command"]
    assert "--channel-topics=-100999:general" in cmd
    assert "--channel-topics=-10007:5,9" in cmd
    assert len(out["jsonl_paths"]) == 1


# ---------------------------------------------------------------------------
# Identity / trust
# ---------------------------------------------------------------------------

def test_trust_lookup_untrust_cycle(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)

    trust_fn = _unwrap(mcp_server.trust_identity)
    lookup_fn = _unwrap(mcp_server.lookup_identity)
    list_fn = _unwrap(mcp_server.list_trusted_identities)
    untrust_fn = _unwrap(mcp_server.untrust_identity)

    # Trust a user.
    r = trust_fn(user_id=42, identity="james", username="james_tg")
    assert r["ok"] is True

    # Lookup resolves correctly.
    r = lookup_fn(user_id=42)
    assert r["trusted"] is True
    assert r["identity"] == "james"
    assert r["row"]["username"] == "james_tg"

    # List returns the binding.
    rows = list_fn()
    assert len(rows) == 1
    assert rows[0]["user_id"] == 42

    # Unknown user is not trusted.
    r = lookup_fn(user_id=99)
    assert r["trusted"] is False
    assert r["identity"] is None

    # Untrust removes it.
    r = untrust_fn(user_id=42)
    assert r["removed"] == 1
    assert lookup_fn(user_id=42)["trusted"] is False


def test_trust_identity_idempotent(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)
    fn = _unwrap(mcp_server.trust_identity)

    fn(user_id=7, identity="alice", username="alice1")
    fn(user_id=7, identity="alice", username="alice2")  # update

    list_fn = _unwrap(mcp_server.list_trusted_identities)
    rows = list_fn(identity="alice")
    assert len(rows) == 1  # upsert, not duplicate
    assert rows[0]["username"] == "alice2"


def test_untrust_nonexistent(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)
    fn = _unwrap(mcp_server.untrust_identity)
    r = fn(user_id=999)
    assert r["removed"] == 0


# ---------------------------------------------------------------------------
# download_media — error paths only (no live bot call)
# ---------------------------------------------------------------------------

def test_download_media_no_such_message(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(mcp_server, "_bot", object())  # bot present but unused
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.download_media)
    with pytest.raises(ValueError, match="no inbound message"):
        asyncio.run(fn(message_id=9999))


def test_download_media_no_media_field(monkeypatch, tmp_path):
    _reload_data_dir(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO messages "
                 "(telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (1, -100, 'text only', 1, 'in')")
    conn.close()

    monkeypatch.setattr(mcp_server, "_bot", object())
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.download_media)
    with pytest.raises(ValueError, match="no media"):
        asyncio.run(fn(message_id=1))


# ---------------------------------------------------------------------------
# Fix 2: drift-proof all-membership tail — live-derive channels from DB
# ---------------------------------------------------------------------------

def test_get_tail_command_no_args_returns_single_per_bot_file(monkeypatch, tmp_path):
    """With no args, always returns the single per-bot file regardless of DB contents."""
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="mybot")
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})

    conn = db.connect()
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (1, -100999, 'group msg', 1, 'in')")
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (2, 174969502, 'dm msg', 2, 'in')")
    conn.close()

    fn = _unwrap(mcp_server.get_tail_command)
    out = fn()
    assert len(out["jsonl_paths"]) == 1
    assert "mybot.jsonl" in out["jsonl_paths"][0]


def test_get_tail_command_fresh_install_returns_per_bot_file(monkeypatch, tmp_path):
    """Even on fresh install with no messages, the per-bot file path is returned."""
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="mybot")
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    db.connect().close()

    fn = _unwrap(mcp_server.get_tail_command)
    out = fn()
    assert len(out["jsonl_paths"]) == 1
    assert "mybot.jsonl" in out["jsonl_paths"][0]


def test_get_tail_command_new_dm_still_single_file(monkeypatch, tmp_path):
    """New DMs don't change the file count — all chats land in the per-bot file."""
    _reload_data_dir(tmp_path, monkeypatch, bot_slug="mybot")
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})

    conn = db.connect()
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (1, -100999, 'hi', 1, 'in')")
    conn.close()

    fn = _unwrap(mcp_server.get_tail_command)
    out1 = fn()
    assert len(out1["jsonl_paths"]) == 1

    conn = db.connect()
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (2, 999888777, 'new dm', 2, 'in')")
    conn.close()

    out2 = fn()
    assert len(out2["jsonl_paths"]) == 1  # still one per-bot file


def test_all_known_chat_ids_db_helper(monkeypatch, tmp_path):
    """all_known_chat_ids() returns all distinct chat_ids from the messages table."""
    _reload_data_dir(tmp_path, monkeypatch)

    conn = db.connect()
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (1, -100, 'a', 1, 'in')")
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (2, 42, 'b', 2, 'in')")
    conn.execute("INSERT INTO messages (telegram_msg_id, chat_id, text, ts, direction) "
                 "VALUES (3, -100, 'c', 3, 'out')")  # duplicate chat_id
    conn.close()

    ids = db.all_known_chat_ids()
    assert set(ids) == {-100, 42}  # distinct; duplicate deduplicated


def test_channel_topics_negative_id_uses_equals_form(monkeypatch):
    """--channel-topics for negative chat_ids uses the = form to avoid argparse
    treating the negative value as an option flag."""
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn(channels=[
        {"chat_id": -1003730692254, "topics": "general"},
    ])
    cmd = out["command"]
    # Must be the = form; the space form would fail argparse.
    assert "--channel-topics=-1003730692254:general" in cmd
    assert "--channel-topics -1003730692254" not in cmd


def test_chat_id_from_path_handles_bot_keyed_filename():
    """chat_id_from_path extracts the chat_id from bot-keyed filenames like
    <chat_id>__<bot_slug>.jsonl — used when bot-keyed DM files are tailed."""
    import tg_local_client.tail as tail

    assert tail.chat_id_from_path("/data/channels/174969502__glen.jsonl") == 174969502
    assert tail.chat_id_from_path("/data/channels/174969502__brass-otter.jsonl") == 174969502
    # Plain scheme still works.
    assert tail.chat_id_from_path("/data/channels/-1003730692254.jsonl") == -1003730692254
    assert tail.chat_id_from_path("/data/channels/42.jsonl") == 42
    # Non-integer stem → None.
    assert tail.chat_id_from_path("/data/channels/channels") is None
