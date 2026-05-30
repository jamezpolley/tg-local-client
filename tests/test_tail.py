"""Tail: follow-from-EOF, new-line emission, and in-process filtering."""
import json

import pytest

import tg_local_client.tail as tail
import tg_local_client.triage as triage


def _emit_lines(capsys, lines, **kw):
    """Push lines through the real _emit filtering path and return what reached stdout."""
    has_filters = any(v is not None and v is not False for v in kw.values())
    for ln in lines:
        tail._emit(ln, has_filters=has_filters, **kw)
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln]


# ---------------------------------------------------------------------------
# Basic follow-from-EOF
# ---------------------------------------------------------------------------

def test_tail_emits_only_lines_appended_after_start(tmp_path):
    f = tmp_path / "chan.jsonl"
    f.write_text(json.dumps({"text": "old", "from_username": "a"}) + "\n")

    follower = tail._Follower(f)
    assert follower.read_new_lines() == []  # opens, seeks to end
    f.write_text(f.read_text() + json.dumps({"text": "new", "from_username": "a"}) + "\n")
    lines = follower.read_new_lines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "new"


# ---------------------------------------------------------------------------
# Deterministic filters
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Self-exclude filter
# ---------------------------------------------------------------------------

def test_exclude_from_user_id(capsys):
    lines = [
        json.dumps({"text": "mine", "from_user_id": 42}),
        json.dumps({"text": "theirs", "from_user_id": 99}),
    ]
    emitted = _emit_lines(capsys, lines, from_username=None, message_thread_id=None,
                          exclude_from_user_id=[42])
    assert len(emitted) == 1
    assert json.loads(emitted[0])["text"] == "theirs"


def test_exclude_from_user_id_none_excluded(capsys):
    lines = [json.dumps({"text": "hi", "from_user_id": 99})]
    emitted = _emit_lines(capsys, lines, from_username=None, message_thread_id=None,
                          exclude_from_user_id=None)
    assert len(emitted) == 1


# ---------------------------------------------------------------------------
# Per-channel topic specs
# ---------------------------------------------------------------------------

def test_topic_spec_all():
    assert tail._parse_topic_spec("all") is None
    assert tail._parse_topic_spec("") is None


def test_topic_spec_general():
    assert tail._parse_topic_spec("general") == "general"


def test_topic_spec_ids():
    assert tail._parse_topic_spec("7") == [7]
    assert tail._parse_topic_spec("7,9") == [7, 9]


def test_topic_matches_all():
    rec = {"message_thread_id": 5}
    assert tail._topic_matches(rec, None)  # None = all topics


def test_topic_matches_general():
    assert tail._topic_matches({"message_thread_id": None}, "general")
    assert not tail._topic_matches({"message_thread_id": 5}, "general")


def test_topic_matches_specific_ids():
    assert tail._topic_matches({"message_thread_id": 5}, [5, 9])
    assert not tail._topic_matches({"message_thread_id": 7}, [5, 9])


def test_channel_topics_per_follower(capsys, tmp_path):
    """Records from a channel constrained to 'general' drop threaded messages."""
    f = tmp_path / "-100100.jsonl"
    lines = [
        json.dumps({"text": "general", "message_thread_id": None, "chat_id": -100100}),
        json.dumps({"text": "thread", "message_thread_id": 7, "chat_id": -100100}),
    ]
    channel_topics = {-100100: "general"}
    for ln in lines:
        rec = json.loads(ln)
        spec = channel_topics.get(rec.get("chat_id"))
        tail._emit(ln, has_filters=True, from_username=None, message_thread_id=None,
                   topic_spec=spec)
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert json.loads(out[0])["text"] == "general"


# ---------------------------------------------------------------------------
# Wake-on / mention / trusted-humans
# ---------------------------------------------------------------------------

def test_wake_on_mention(capsys):
    lines = [
        json.dumps({"text": "hey @mybot do this", "from_user_id": 1}),
        json.dumps({"text": "ignore this", "from_user_id": 2}),
    ]
    emitted = _emit_lines(capsys, lines, from_username=None, message_thread_id=None,
                          wake_on=["mention"], mention_username="mybot")
    assert len(emitted) == 1
    assert "mybot" in json.loads(emitted[0])["text"]


def test_wake_on_trusted_humans(capsys):
    lines = [
        json.dumps({"text": "from james", "from_user_id": 42}),
        json.dumps({"text": "from unknown", "from_user_id": 99}),
    ]
    emitted = _emit_lines(capsys, lines, from_username=None, message_thread_id=None,
                          wake_on=["trusted_humans"], trusted_user_ids=[42])
    assert len(emitted) == 1
    assert json.loads(emitted[0])["from_user_id"] == 42


def test_wake_on_either_clause_passes(capsys):
    """A record passes if EITHER mention OR trusted_human is satisfied."""
    lines = [
        json.dumps({"text": "@mybot hello", "from_user_id": 99}),  # mention match
        json.dumps({"text": "direct ask", "from_user_id": 42}),    # trusted match
        json.dumps({"text": "unrelated", "from_user_id": 7}),      # no match
    ]
    emitted = _emit_lines(capsys, lines, from_username=None, message_thread_id=None,
                          wake_on=["mention", "trusted_humans"],
                          mention_username="mybot", trusted_user_ids=[42])
    assert len(emitted) == 2


def test_is_mention_case_insensitive():
    rec = {"text": "hey @MyBot please do it"}
    assert tail._is_mention(rec, "mybot")
    assert tail._is_mention(rec, "MyBot")
    assert not tail._is_mention(rec, "otherbot")


# ---------------------------------------------------------------------------
# Parser: new flags round-trip
# ---------------------------------------------------------------------------

def test_parser_accepts_exclude_user_id():
    args = tail.build_parser().parse_args(
        ["/tmp/chan.jsonl", "--exclude-from-user-id", "42",
         "--exclude-from-user-id", "99"]
    )
    assert args.exclude_from_user_id == [42, 99]


def test_parser_accepts_channel_topics():
    args = tail.build_parser().parse_args(
        ["/tmp/chan.jsonl", "--channel-topics=-100100:general",
         "--channel-topics=-10007:5,9"]
    )
    topics = tail._parse_channel_topics(args.channel_topics)
    assert topics[-100100] == "general"
    assert topics[-10007] == [5, 9]


def test_parser_accepts_wake_on():
    args = tail.build_parser().parse_args(
        ["/tmp/chan.jsonl", "--wake-on", "mention",
         "--wake-on", "trusted_humans",
         "--mention-username", "mybot",
         "--trusted-user-id", "42"]
    )
    assert "mention" in args.wake_on
    assert "trusted_humans" in args.wake_on
    assert args.mention_username == "mybot"
    assert args.trusted_user_id == [42]


def test_parser_accepts_triage_flags():
    args = tail.build_parser().parse_args(
        ["/tmp/chan.jsonl", "--triage",
         "--triage-role", "the upload agent",
         "--triage-state-file", "/tmp/state.txt",
         "--triage-model", "claude-haiku-4-5"]
    )
    assert args.triage is True
    assert args.triage_role == "the upload agent"
    assert args.triage_state_file == "/tmp/state.txt"
    assert args.triage_model == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Triage integration (fail-safe path — no live claude)
# ---------------------------------------------------------------------------

def test_triage_config_builds_from_args():
    args = tail.build_parser().parse_args(
        ["/tmp/chan.jsonl", "--triage", "--triage-role", "test agent"]
    )
    cfg = tail._build_triage_config(args)
    assert cfg is not None
    assert cfg.role == "test agent"
    assert cfg.state_file is None


def test_triage_config_none_when_flag_absent():
    args = tail.build_parser().parse_args(["/tmp/chan.jsonl"])
    assert tail._build_triage_config(args) is None


def test_triage_fail_safe_on_invoke_error(capsys):
    """Any error from the claude subprocess → ACT (line is emitted)."""
    cfg = triage.TriageConfig(role="test")

    def _fail_invoke(prompt, model, timeout):
        raise RuntimeError("claude not found")

    line = json.dumps({"text": "some message", "from_username": "user"})
    result = triage.should_act("some message", cfg, invoke=_fail_invoke)
    assert result is True  # fail-safe: error → ACT


def test_triage_skip_decision(capsys):
    """SKIP output from the classifier suppresses the line."""
    cfg = triage.TriageConfig(role="test")

    def _skip_invoke(prompt, model, timeout):
        return "SKIP"

    assert triage.should_act("irrelevant message", cfg, invoke=_skip_invoke) is False


def test_triage_act_decision():
    cfg = triage.TriageConfig(role="test")

    def _act_invoke(prompt, model, timeout):
        return "ACT"

    assert triage.should_act("relevant message", cfg, invoke=_act_invoke) is True


def test_triage_empty_output_is_act():
    """Empty or whitespace-only output → fail-safe ACT."""
    cfg = triage.TriageConfig(role="test")

    def _empty_invoke(prompt, model, timeout):
        return ""

    assert triage.should_act("x", cfg, invoke=_empty_invoke) is True


def test_triage_filters_line_in_emit(capsys):
    """_emit suppresses a line when triage returns SKIP."""
    cfg = triage.TriageConfig(role="test")
    original_should_act = triage.should_act

    def _patched_should_act(message, config, invoke=None):
        return False  # always SKIP

    import tg_local_client.tail as _tail
    original = _tail._triage_mod.should_act
    _tail._triage_mod.should_act = _patched_should_act
    try:
        line = json.dumps({"text": "skip me", "from_username": "u"})
        _tail._emit(line, has_filters=False, from_username=None,
                    message_thread_id=None, triage_config=cfg)
        out = capsys.readouterr().out
        assert out == ""
    finally:
        _tail._triage_mod.should_act = original


def test_triage_passes_line_in_emit(capsys):
    """_emit emits a line when triage returns ACT."""
    cfg = triage.TriageConfig(role="test")

    import tg_local_client.tail as _tail
    original = _tail._triage_mod.should_act
    _tail._triage_mod.should_act = lambda msg, config, invoke=None: True
    try:
        line = json.dumps({"text": "act on me", "from_username": "u"})
        _tail._emit(line, has_filters=False, from_username=None,
                    message_thread_id=None, triage_config=cfg)
        out = capsys.readouterr().out
        assert "act on me" in out
    finally:
        _tail._triage_mod.should_act = original


# ---------------------------------------------------------------------------
# chat_id_from_path
# ---------------------------------------------------------------------------

def test_chat_id_from_path_negative():
    assert tail.chat_id_from_path("/data/channels/-100123456.jsonl") == -100123456


def test_chat_id_from_path_positive():
    assert tail.chat_id_from_path("/data/channels/174969502.jsonl") == 174969502


def test_chat_id_from_path_non_integer():
    assert tail.chat_id_from_path("/data/channels/inbound.jsonl") is None
