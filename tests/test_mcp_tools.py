"""MCP tools: send payload (incl. group default + empty-group guard), tail flatness."""
import asyncio

import pytest

import tg_local_client.mcp_server as mcp_server


def _unwrap(tool):
    """FastMCP wraps functions in a Tool/FunctionTool; get the underlying callable."""
    return getattr(tool, "fn", tool)


def test_send_message_builds_correct_payload(monkeypatch):
    sent_calls = {}

    class FakeBot:
        async def send_message(self, chat_id, text, message_thread_id=None):
            sent_calls.update(chat_id=chat_id, text=text,
                              message_thread_id=message_thread_id)

            class Sent:
                message_id = 555
            return Sent()

    monkeypatch.setattr(mcp_server, "_bot", FakeBot())
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999],
                                                "token_env_var": "TG_BOT_TOKEN"})
    # Don't actually touch SQLite.
    monkeypatch.setattr(mcp_server, "record_outbound",
                        lambda mid, cid, txt: {"telegram_msg_id": mid, "chat_id": cid})

    fn = _unwrap(mcp_server.send_message)
    result = asyncio.run(fn(text="ack: on it"))
    # Defaulted to the first configured group.
    assert sent_calls["chat_id"] == -100999
    assert sent_calls["text"] == "ack: on it"
    assert result["telegram_msg_id"] == 555


def test_send_message_raises_when_no_group_configured(monkeypatch):
    class FakeBot:
        async def send_message(self, **kw):  # pragma: no cover - should not be called
            raise AssertionError("send should not happen with no target")

    monkeypatch.setattr(mcp_server, "_bot", FakeBot())
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [],
                                                "token_env_var": "TG_BOT_TOKEN"})
    fn = _unwrap(mcp_server.send_message)
    with pytest.raises(RuntimeError, match="group_chat_ids"):
        asyncio.run(fn(text="nowhere to go"))


def test_get_tail_command_is_flat_and_pipe_free(monkeypatch):
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn()
    cmd = out["command"]
    # No shell operators that would trip the permission prompt.
    for forbidden in ("|", "&&", ";", "$(", "jq", ">", "<"):
        assert forbidden not in cmd, f"command contains forbidden token {forbidden!r}: {cmd}"
    assert cmd.startswith("uv run --directory ")
    assert "tg-local-tail" in cmd
    assert out["jsonl_path"].endswith("-100999.jsonl")


def test_get_tail_command_filter_sanitised(monkeypatch):
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": [-100999]})
    fn = _unwrap(mcp_server.get_tail_command)
    out = fn(from_username="bad; rm -rf /")
    # Non-alphanumerics stripped → no injection into the flat command.
    assert "rm" in out["command"]  # letters survive
    assert ";" not in out["command"]
    assert " " not in out["command"].split("--from-username ")[1].split()[0]


def test_get_tail_command_raises_when_no_group(monkeypatch):
    monkeypatch.setattr(mcp_server, "_config", {"group_chat_ids": []})
    fn = _unwrap(mcp_server.get_tail_command)
    with pytest.raises(RuntimeError, match="group_chat_ids"):
        fn()
