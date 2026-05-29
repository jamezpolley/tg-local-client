"""Tail: follow-from-EOF, new-line emission, and in-process filtering."""
import json

import tg_local_client.tail as tail


def _emit_lines(capsys, lines, **kw):
    """Push lines through the real _emit filtering path and return what reached stdout."""
    has_filters = any(v is not None for v in kw.values())
    for ln in lines:
        tail._emit(ln, has_filters=has_filters, **kw)
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln]


def test_tail_emits_only_lines_appended_after_start(tmp_path):
    f = tmp_path / "chan.jsonl"
    f.write_text(json.dumps({"text": "old", "from_username": "a"}) + "\n")

    # First open seeks to EOF (no emit), then we append a line and read again.
    follower = tail._Follower(f)
    assert follower.read_new_lines() == []  # opens, seeks to end
    f.write_text(f.read_text() + json.dumps({"text": "new", "from_username": "a"}) + "\n")
    lines = follower.read_new_lines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "new"


def test_tail_username_filter(capsys):
    lines = [
        json.dumps({"text": "keep", "from_username": "wanted"}),
        json.dumps({"text": "drop", "from_username": "other"}),
    ]
    emitted = _emit_lines(capsys, lines, from_username="wanted", message_thread_id=None)
    assert len(emitted) == 1
    assert json.loads(emitted[0])["text"] == "keep"


def test_tail_thread_id_filter(capsys):
    lines = [
        json.dumps({"text": "t5", "message_thread_id": 5}),
        json.dumps({"text": "t9", "message_thread_id": 9}),
    ]
    emitted = _emit_lines(capsys, lines, from_username=None, message_thread_id=5)
    assert len(emitted) == 1
    assert json.loads(emitted[0])["text"] == "t5"


def test_tail_no_filter_passes_lines_through(capsys):
    lines = ["raw-line-1", "raw-line-2"]
    emitted = _emit_lines(capsys, lines, from_username=None, message_thread_id=None)
    assert emitted == ["raw-line-1", "raw-line-2"]


def test_matches_predicate():
    rec = {"from_username": "x", "message_thread_id": 3}
    assert tail._matches(rec, from_username="x", message_thread_id=3)
    assert not tail._matches(rec, from_username="y", message_thread_id=3)
    assert not tail._matches(rec, from_username="x", message_thread_id=4)
    assert tail._matches(rec, from_username=None, message_thread_id=None)
