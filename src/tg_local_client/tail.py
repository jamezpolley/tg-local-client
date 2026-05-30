"""Pure-Python `tail -n0 -F` replacement for the local Telegram client monitor.

`get_tail_command` (mcp_server) returns an invocation of this entry point instead
of a `tail … | jq …` shell pipe. The pipe + jq predicate trips Claude Code's
Bash permission prompt on every monitor setup; a single flat command matches one
simple allowlist entry and never prompts.

Behaviour mirrors `tail -n0 -F <files> | jq -c --unbuffered 'select(<filters>)'`:

- follow one or more per-channel JSONL files from their current end (`-n0`),
- emit each NEW line that matches the optional fine-filters as one compact JSON
  line to stdout, flushing per line so Claude Code's Monitor tool sees events live,
- re-open files on rotation/truncation (`-F`),
- pick up files that do not yet exist (the channel file is created lazily on the
  first message), same as `tail -F` waiting for a path to appear.

Filtering is done in Python on the parsed JSON record — no `jq`, no subprocess,
no shell operators. A line that is not valid JSON is passed through verbatim
(matching `tail`'s permissiveness) only when there are no filters; with filters
active, unparseable lines are skipped (they cannot satisfy a predicate), which
mirrors `jq -c 'select(...)'` dropping non-matching input.

## Deterministic addressing layer (ALL OPT-IN)

These let an agent declare what it wants woken on so it can retire hand-rolled jq.
Every one is OPT-IN: omit them all and behaviour is exactly as before (all files
given, every sender). A declared filter is a CHEAP FIRST LAYER, never the sole
relevance gate — a strict filter can drop a genuinely-relevant message, so Haiku
relevance triage stays a separate later layer the agent owns.

  --exclude-from-user-id   drop records from these user_ids (repeatable)
  --wake-on mention|trusted_humans  sender/@-mention allow-list
  --mention-username       bot's own @username (without @) for mention clause
  --trusted-user-id        a trusted human user_id (repeatable)
  --channel-topics CHAT_ID:SPEC   per-channel topic scope (repeatable)

## Opt-in Haiku ACT/SKIP triage layer

  --triage                 enable the smart residue cut
  --triage-role            agent identity string
  --triage-state-file      path to agent's waiting-on state file
  --triage-model           claude model alias (default: haiku)
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Union

from . import triage as _triage_mod


POLL_INTERVAL = 0.25  # seconds between read attempts; matches tail -F responsiveness

# A topic spec describes which forum-topic threads to keep WITHIN one channel:
#   None         → all topics (default; no topic scoping)
#   "general"    → general topic only (message_thread_id is null/absent)
#   [int, ...]   → only these specific thread ids
TopicSpec = Union[None, str, list]


def chat_id_from_path(path: Union[str, Path]) -> Optional[int]:
    """Derive the channel chat_id from a per-channel JSONL filename (<chat_id>.jsonl).

    Returns None if the stem isn't a plain integer (e.g. a non-standard path),
    in which case per-channel topic specs can't be keyed to this file and the
    global default applies.
    """
    stem = Path(path).stem
    try:
        return int(stem)
    except (TypeError, ValueError):
        return None


def _parse_topic_spec(raw: str) -> TopicSpec:
    """Parse a CLI topic-spec token into a TopicSpec.

    "all" → None (all topics), "general" → "general", "7" or "7,9" → [7, 9].
    """
    raw = raw.strip()
    if raw in ("", "all"):
        return None
    if raw == "general":
        return "general"
    return [int(p) for p in raw.split(",") if p.strip() != ""]


def _topic_matches(record: dict, spec: TopicSpec) -> bool:
    """True if the record's forum-topic thread satisfies the per-channel topic spec."""
    if spec is None:
        return True
    thread = record.get("message_thread_id")
    if spec == "general":
        return thread is None
    # spec is a list of allowed thread ids.
    return thread in spec


def _is_mention(record: dict, mention_username: Optional[str]) -> bool:
    """True if the record's text @-mentions mention_username (case-insensitive).

    mention_username is the bot's own @username WITHOUT the leading '@'. The match
    is a word-bounded `@username` substring on the message text — Telegram doesn't
    persist message entities in the client record, so this is a deliberate text scan.
    """
    if not mention_username:
        return False
    text = record.get("text") or ""
    pattern = r"@" + re.escape(mention_username) + r"\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _is_trusted_human(record: dict, trusted_ids: Optional[list]) -> bool:
    """True if the record's sender is one of the trusted human user_ids."""
    if not trusted_ids:
        return False
    return record.get("from_user_id") in trusted_ids


class _Follower:
    """Tracks one file: open handle, inode, and position, with rotation handling."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.chat_id = chat_id_from_path(path)
        self._fh = None
        self._inode = None

    def _try_open(self) -> None:
        try:
            fh = open(self.path, "r", encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return
        # Seek to end: -n0 means we only emit lines appended AFTER we start.
        fh.seek(0, os.SEEK_END)
        try:
            self._inode = os.fstat(fh.fileno()).st_ino
        except OSError:
            self._inode = None
        self._fh = fh

    def _rotated(self) -> bool:
        """True if the path now points at a different inode than our open handle,
        or the file shrank below our position (truncation)."""
        try:
            st = os.stat(self.path)
        except (FileNotFoundError, PermissionError):
            return False  # path vanished; keep current handle until it returns
        if self._inode is not None and st.st_ino != self._inode:
            return True
        try:
            if self._fh is not None and st.st_size < self._fh.tell():
                return True
        except (OSError, ValueError):
            return False
        return False

    def read_new_lines(self) -> list[str]:
        """Return any complete new lines appended since the last call.

        Handles the file not existing yet (waits), rotation (re-open from start of
        the new file), and truncation (re-open). Partial trailing lines are not
        emitted until their terminating newline arrives.
        """
        if self._fh is None:
            self._try_open()
            return []

        if self._rotated():
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            # Re-open and read the rotated/truncated file from its BEGINNING, so
            # we don't lose lines written between rotation and our next poll.
            try:
                fh = open(self.path, "r", encoding="utf-8", errors="replace")
            except (FileNotFoundError, IsADirectoryError, PermissionError):
                self._inode = None
                return []
            try:
                self._inode = os.fstat(fh.fileno()).st_ino
            except OSError:
                self._inode = None
            self._fh = fh

        lines: list[str] = []
        while True:
            line = self._fh.readline()
            if not line:
                break
            if line.endswith("\n"):
                lines.append(line[:-1])
            else:
                # Partial line: rewind so we re-read it once it's complete.
                self._fh.seek(self._fh.tell() - len(line))
                break
        return lines

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def _matches(record: dict, *, from_username: Optional[str],
             message_thread_id: Optional[int],
             exclude_from_user_id: Optional[list[int]] = None,
             topic_spec: TopicSpec = None,
             wake_on: Optional[list[str]] = None,
             mention_username: Optional[str] = None,
             trusted_user_ids: Optional[list[int]] = None) -> bool:
    if from_username is not None and record.get("from_username") != from_username:
        return False
    if message_thread_id is not None and record.get("message_thread_id") != message_thread_id:
        return False
    # Per-channel topic spec (general-only / specific-ids / all).
    if not _topic_matches(record, topic_spec):
        return False
    # Self-exclude: drop records whose from_user_id is one of the excluded ids.
    if exclude_from_user_id and record.get("from_user_id") in exclude_from_user_id:
        return False
    # Sender / @-mention allow-list (opt-in relevance pre-filter). If wake_on is
    # set, the record must satisfy at least ONE enabled clause:
    #   "mention"        → text @-mentions this bot's username
    #   "trusted_humans" → sender is a trusted human user_id
    # This is the CHEAP FIRST LAYER, never the sole relevance gate — a strict
    # filter can drop a genuinely-relevant message, so Haiku triage stays a
    # separate later layer the agent owns.
    if wake_on:
        ok = False
        if "mention" in wake_on and _is_mention(record, mention_username):
            ok = True
        if not ok and "trusted_humans" in wake_on and \
                _is_trusted_human(record, trusted_user_ids):
            ok = True
        if not ok:
            return False
    return True


def _triage_passes(line: str, triage_config) -> bool:
    """Run the OPT-IN Haiku ACT/SKIP layer on a line that already survived the
    deterministic filters. True = emit (ACT), False = suppress (SKIP).

    No triage_config → always pass (layer is off, behaviour unchanged). The
    classifier itself is fail-safe (any error/malformed output → ACT), so this
    can only ever ADD suppression on top of the deterministic cut, never widen it.
    """
    if triage_config is None:
        return True
    message = _triage_mod.message_text_for_triage(line)
    return _triage_mod.should_act(message, triage_config)


def _emit(line: str, *, has_filters: bool, from_username: Optional[str],
          message_thread_id: Optional[int],
          exclude_from_user_id: Optional[list[int]] = None,
          topic_spec: TopicSpec = None,
          wake_on: Optional[list[str]] = None,
          mention_username: Optional[str] = None,
          trusted_user_ids: Optional[list[int]] = None,
          triage_config=None) -> None:
    """Apply filters to one raw JSONL line and print it (compact) if it matches.

    The deterministic filters are the free coarse cut. When `triage_config` is set
    (opt-in --triage), a line that survives them then goes through the Haiku
    ACT/SKIP layer and is emitted only if ACT. With no triage_config the smart
    layer is inert and behaviour is byte-identical to the deterministic-only path.
    """
    if not has_filters:
        # No deterministic filters AND no triage → fast pass-through.
        if triage_config is None:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            return
        # Triage-only mode: classify, emit verbatim if ACT.
        if _triage_passes(line, triage_config):
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        return
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return  # filters active → can't match an unparseable line; drop it
    if not isinstance(record, dict):
        return
    if _matches(record, from_username=from_username,
                message_thread_id=message_thread_id,
                exclude_from_user_id=exclude_from_user_id,
                topic_spec=topic_spec,
                wake_on=wake_on,
                mention_username=mention_username,
                trusted_user_ids=trusted_user_ids):
        # Deterministic layer passed; now the opt-in smart residue cut.
        if not _triage_passes(line, triage_config):
            return
        sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tg-local-tail",
        description="Follow one or more channel JSONL files and emit new matching "
                    "lines (pure-Python tail -n0 -F + jq select).",
    )
    p.add_argument("files", nargs="+", help="channel JSONL file path(s) to follow")
    p.add_argument("--from-username", default=None,
                   help="only emit records whose from_username equals this value")
    p.add_argument("--message-thread-id", type=int, default=None,
                   help="only emit records in this forum-topic thread id")
    p.add_argument("--exclude-from-user-id", type=int, action="append", default=None,
                   help="drop records whose from_user_id equals this id (repeatable). "
                        "Used to suppress a bot's own sends echoed back.")
    p.add_argument("--channel-topics", action="append", default=None,
                   metavar="CHAT_ID:SPEC",
                   help="per-channel topic scope (repeatable). SPEC is 'all' (default), "
                        "'general' (general topic only — message_thread_id null), or a "
                        "comma-separated list of thread ids (e.g. '7,9').")
    p.add_argument("--wake-on", action="append", default=None,
                   choices=["mention", "trusted_humans"],
                   help="opt-in relevance pre-filter (repeatable). 'mention' = wake only "
                        "on messages @-mentioning this bot; 'trusted_humans' = wake on "
                        "messages from a trusted human user_id. If both given, EITHER "
                        "matching wakes. Cheap first layer, not the sole relevance gate.")
    p.add_argument("--mention-username", default=None,
                   help="this bot's @username (without '@') for the 'mention' wake-on clause")
    p.add_argument("--trusted-user-id", type=int, action="append", default=None,
                   help="a trusted human's Telegram user_id for the 'trusted_humans' "
                        "wake-on clause (repeatable).")
    # Opt-in Haiku ACT/SKIP triage layer.
    p.add_argument("--triage", action="store_true", default=False,
                   help="enable the OPT-IN Haiku ACT/SKIP relevance layer: for each "
                        "line that survives the deterministic filters, ask a cheap "
                        "`claude -p --model haiku` whether the agent must ACT, and "
                        "emit the line only if ACT. Off by default. "
                        "Fail-safe: any error/malformed/empty Haiku output → ACT.")
    p.add_argument("--triage-role", default=None,
                   help="short role/identity string for the agent, fed into the Haiku "
                        "prompt (e.g. 'the pod-upload agent that publishes episodes'). "
                        "Required when --triage is set.")
    p.add_argument("--triage-state-file", default=None,
                   help="path to a file holding the agent's current waiting-on state "
                        "(what it's blocked on / expecting); read FRESH per message and "
                        "fed into the Haiku prompt.")
    p.add_argument("--triage-model", default=_triage_mod.DEFAULT_TRIAGE_MODEL,
                   help="claude model alias for the relevance call (default: haiku).")
    return p


def _build_triage_config(args):
    """Build a triage.TriageConfig from parsed args, or None when --triage is off."""
    if not getattr(args, "triage", False):
        return None
    role = args.triage_role or "(no role provided)"
    state_file = Path(args.triage_state_file) if args.triage_state_file else None
    return _triage_mod.TriageConfig(
        role=role,
        state_file=state_file,
        model=args.triage_model,
    )


def _parse_channel_topics(tokens: Optional[list]) -> dict:
    """Parse repeated CHAT_ID:SPEC tokens into {chat_id: TopicSpec}."""
    out: dict = {}
    for tok in tokens or []:
        if ":" not in tok:
            continue
        cid_str, spec_str = tok.split(":", 1)
        try:
            cid = int(cid_str)
        except ValueError:
            continue
        out[cid] = _parse_topic_spec(spec_str)
    return out


def stream(files: list[str], *, from_username: Optional[str] = None,
           message_thread_id: Optional[int] = None,
           exclude_from_user_id: Optional[list[int]] = None,
           channel_topics: Optional[dict] = None,
           wake_on: Optional[list[str]] = None,
           mention_username: Optional[str] = None,
           trusted_user_ids: Optional[list[int]] = None,
           triage_config=None,
           poll_interval: float = POLL_INTERVAL,
           max_iterations: Optional[int] = None) -> None:
    """Follow the given files forever, emitting new matching lines to stdout.

    channel_topics: {chat_id: TopicSpec} mapping a channel's chat_id to its
        per-channel topic scope. Each follower derives its chat_id from its
        filename and looks up its spec here; channels not listed default to
        all-topics.
    wake_on / mention_username / trusted_user_ids: opt-in sender/@-mention
        relevance pre-filter (see _matches).
    triage_config: opt-in triage.TriageConfig enabling the Haiku ACT/SKIP smart
        residue cut on lines that survive the deterministic filters. None = off
        (behaviour byte-identical to the deterministic-only path).

    max_iterations bounds the poll loop (used by tests); None = run until killed.
    """
    channel_topics = channel_topics or {}
    has_filters = any(v is not None for v in (from_username, message_thread_id)) \
        or bool(exclude_from_user_id) \
        or bool(channel_topics) \
        or bool(wake_on)
    followers = [_Follower(Path(f)) for f in files]
    iterations = 0
    try:
        while True:
            for follower in followers:
                topic_spec = channel_topics.get(follower.chat_id)
                for line in follower.read_new_lines():
                    _emit(line, has_filters=has_filters,
                          from_username=from_username,
                          message_thread_id=message_thread_id,
                          exclude_from_user_id=exclude_from_user_id,
                          topic_spec=topic_spec,
                          wake_on=wake_on,
                          mention_username=mention_username,
                          trusted_user_ids=trusted_user_ids,
                          triage_config=triage_config)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        return
    finally:
        for follower in followers:
            follower.close()


def run(argv: Optional[list[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    stream(
        args.files,
        from_username=args.from_username,
        message_thread_id=args.message_thread_id,
        exclude_from_user_id=args.exclude_from_user_id,
        channel_topics=_parse_channel_topics(args.channel_topics),
        wake_on=args.wake_on,
        mention_username=args.mention_username,
        trusted_user_ids=args.trusted_user_id,
        triage_config=_build_triage_config(args),
    )


if __name__ == "__main__":
    run()
