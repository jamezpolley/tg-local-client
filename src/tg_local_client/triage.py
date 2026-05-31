"""Optional Haiku ACT/SKIP relevance layer for the flat-tail monitor.

The flat tail (`tail.py`) does a CHEAP, DETERMINISTIC coarse cut: per-channel
topic scope, wake-on mention/trusted, self-exclude. Those filters are free but
blunt — they can't read intent. This module adds an OPT-IN smart residue cut:
for each line that already survived the deterministic filters, ask a cheap
Haiku `claude -p` whether the agent must ACT on it, and let the line through to
stdout ONLY if the answer is ACT.

Net effect for the agent: one `tg-local-tail --triage …` command does both
layers, and its Monitor fires only on messages that genuinely concern it.

## Production learnings folded in (lifted from the dex-tg fabric)

- **Neutral cwd + MCP disabled.** The Haiku call runs in `/tmp` with
  `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` so it does NOT load the
  project's CLAUDE.md / MCP servers / skills. Loading them is slow and can make
  the classifier misfire on unrelated project context.
- **Pushed waiting-on state.** A separate `claude -p` context can't read the
  agent's live "what am I blocked on / waiting for" state, so the agent writes it
  to a file and we feed the file's contents into the prompt.
- **Bias toward ACT.** The only failure that matters is a false NEGATIVE —
  SKIPping a message the agent needed. A false positive just costs one wakeup.
  So the prompt is explicit about erring toward ACT, AND any malformed/empty/
  errored Haiku response is treated as ACT (fail-safe; never silently SKIP).

The actual subprocess invocation is isolated in `_invoke_claude` so tests can
monkeypatch it — no live `claude` and no network in the test suite.
"""
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# Default model for the relevance check. Haiku is the cheap/fast tier; the whole
# point of this layer is that it's a per-message micro-call, so cost matters.
DEFAULT_TRIAGE_MODEL = "haiku"

# Neutral working directory for the classifier subprocess: no project CLAUDE.md,
# no project .mcp.json, nothing that would slow or bias the call.
NEUTRAL_CWD = "/tmp"

# Wall-clock ceiling for one classification. If Haiku hangs we fail-safe to ACT
# rather than block the monitor — a stuck triage must never silently swallow a
# message.
DEFAULT_TIMEOUT_SECONDS = 30


@dataclass
class TriageConfig:
    """Everything the Haiku ACT/SKIP check needs, resolved once at startup.

    role:        short description of the agent's identity / responsibility, e.g.
                 "the pod-upload agent: you publish finished podcast episodes".
    state_file:  path to a file the agent keeps current with its waiting-on state
                 (what it's blocked on / expecting). Read FRESH per message so an
                 agent can update it mid-run and the next classification sees it.
    model:       claude model alias for the relevance call (default "haiku").
    timeout:     per-call wall-clock ceiling in seconds.
    """
    role: str
    state_file: Optional[Path] = None
    model: str = DEFAULT_TRIAGE_MODEL
    timeout: int = DEFAULT_TIMEOUT_SECONDS


def _read_state(state_file: Optional[Path]) -> str:
    """Read the agent's current waiting-on state, fresh, fail-soft.

    Returns a short human string. Missing/unreadable file → a neutral marker so
    the prompt still makes sense (and the classifier leans ACT under ambiguity).
    """
    if state_file is None:
        return "(no waiting-on state provided)"
    try:
        text = Path(state_file).read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return "(waiting-on state file not readable)"
    return text or "(waiting-on state file is empty)"


def build_prompt(role: str, waiting_on: str, message: str) -> str:
    """Compose the ACT/SKIP prompt: agent identity + waiting-on state + candidate.

    The prompt is deliberately explicit that a false SKIP is the only costly
    error, so the model errs toward ACT when uncertain.
    """
    return (
        "You are a relevance gate for an autonomous agent. Decide whether the "
        "agent must ACT on one incoming chat message, or can SKIP it.\n\n"
        f"AGENT ROLE / IDENTITY:\n{role}\n\n"
        f"AGENT'S CURRENT WAITING-ON STATE (what it is blocked on or expecting):\n"
        f"{waiting_on}\n\n"
        f"INCOMING MESSAGE:\n{message}\n\n"
        "Answer with exactly one word: ACT or SKIP.\n"
        "- ACT  = the agent needs to see or do something about this message.\n"
        "- SKIP = the message does not concern the agent at all.\n"
        "IMPORTANT: When in doubt, answer ACT. A wrong SKIP makes the agent miss "
        "something it needed (the only failure that matters). A wrong ACT only "
        "costs one harmless wakeup. Bias strongly toward ACT.\n"
        "Reply with ACT or SKIP and nothing else."
    )


def _invoke_claude(prompt: str, model: str, timeout: int) -> str:
    """Run the cheap Haiku ACT/SKIP check and return its raw stdout.

    Isolated so tests can monkeypatch the whole subprocess. Runs in a NEUTRAL cwd
    with MCP fully disabled (empty server config + --strict-mcp-config) so the
    classifier never loads the project's CLAUDE.md / MCP / skills.

    Raises on subprocess failure/timeout; the caller treats any raise as ACT.
    """
    proc = subprocess.run(
        [
            "claude",
            "-p",
            "--model", model,
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            prompt,
        ],
        cwd=NEUTRAL_CWD,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.stdout or ""


def _parse_decision(raw: str) -> bool:
    """Interpret the Haiku output as ACT (True) or SKIP (False).

    FAIL-SAFE: anything that isn't an unambiguous SKIP is treated as ACT. An
    empty, malformed, or chatty response must NEVER silently SKIP a message.
    """
    if raw is None:
        return True
    token = raw.strip().upper()
    if not token:
        return True
    # Only an unambiguous SKIP suppresses (return False). "ACT", noise, or a
    # mixed answer → ACT (return True). False == suppress the message.
    first = token.split()[0].strip(".,:;!?\"'`*")
    if first == "SKIP":
        return False
    return True


def should_act(message: str, config: TriageConfig,
               invoke: Optional[Callable[[str, str, int], str]] = None) -> bool:
    """Return True if the agent must ACT on `message` (emit it), False to SKIP.

    `invoke` lets tests inject a fake `claude -p`; defaults to `_invoke_claude`.
    Any exception from the invocation is caught and treated as ACT (fail-safe).
    """
    invoke = invoke or _invoke_claude
    waiting_on = _read_state(config.state_file)
    prompt = build_prompt(config.role, waiting_on, message)
    try:
        raw = invoke(prompt, config.model, config.timeout)
    except Exception:
        # Subprocess failure, timeout, claude not installed, etc. → fail-safe ACT.
        return True
    return _parse_decision(raw)


def message_text_for_triage(line: str) -> str:
    """Extract the human-meaningful text to hand the classifier from a JSONL line.

    Returns the record's `text` when it has real content; returns "" when `text`
    is absent/empty/whitespace-only. Such records — untagged service events (this
    client's listener writes chat_member_added etc. as a plain `text=""` record,
    with no service marker), caption-less media (stickers/photos with no caption),
    and empty sends — carry nothing to classify. Callers treat "" as a
    deterministic SKIP (see tail._triage_passes).

    Crucially this does NOT fall back to the raw JSON line for empty text: doing so
    handed a textless blob to the bias-to-ACT classifier, waking the agent on every
    such record. Unparseable / non-dict lines DO return the raw line, so they still
    reach the (fail-safe) classifier rather than being silently dropped here.
    """
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    if isinstance(record, dict):
        text = record.get("text")
        return text if isinstance(text, str) and text.strip() else ""
    return line
