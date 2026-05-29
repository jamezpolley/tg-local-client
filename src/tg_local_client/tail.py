"""Pure-Python `tail -n0 -F` replacement for the local Telegram client monitor.

`get_tail_command` (mcp_server) returns an invocation of this entry point instead
of a `tail … | jq …` shell pipe. The pipe + jq predicate trips Claude Code's
Bash permission prompt on every monitor setup; a single flat command matches one
simple allowlist entry and never prompts.

Behaviour mirrors `tail -n0 -F <files> | jq -c 'select(<filters>)'`: follow one or
more per-channel JSONL files from EOF, emit each NEW matching record as one compact
JSON line to stdout (flushed per line), re-open on rotation/truncation, and pick up
files that do not yet exist (the channel file is created lazily on first message).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional


POLL_INTERVAL = 0.25  # seconds between read attempts; matches tail -F responsiveness


class _Follower:
    """Tracks one file: open handle, inode, and position, with rotation handling."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None
        self._inode = None

    def _try_open(self) -> None:
        try:
            fh = open(self.path, "r", encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return
        fh.seek(0, os.SEEK_END)  # -n0: only emit lines appended after we start
        try:
            self._inode = os.fstat(fh.fileno()).st_ino
        except OSError:
            self._inode = None
        self._fh = fh

    def _rotated(self) -> bool:
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
        if self._fh is None:
            self._try_open()
            return []

        if self._rotated():
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
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
             message_thread_id: Optional[int]) -> bool:
    if from_username is not None and record.get("from_username") != from_username:
        return False
    if message_thread_id is not None and record.get("message_thread_id") != message_thread_id:
        return False
    return True


def _emit(line: str, *, has_filters: bool, from_username: Optional[str],
          message_thread_id: Optional[int]) -> None:
    if not has_filters:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(record, dict):
        return
    if _matches(record, from_username=from_username,
                message_thread_id=message_thread_id):
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
    return p


def stream(files: list[str], *, from_username: Optional[str] = None,
           message_thread_id: Optional[int] = None,
           poll_interval: float = POLL_INTERVAL,
           max_iterations: Optional[int] = None) -> None:
    """Follow the given files forever, emitting new matching lines to stdout.

    max_iterations bounds the poll loop (used by tests); None = run until killed.
    """
    has_filters = any(v is not None for v in (from_username, message_thread_id))
    followers = [_Follower(Path(f)) for f in files]
    iterations = 0
    try:
        while True:
            for follower in followers:
                for line in follower.read_new_lines():
                    _emit(line, has_filters=has_filters,
                          from_username=from_username,
                          message_thread_id=message_thread_id)
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
    )


if __name__ == "__main__":
    run()
