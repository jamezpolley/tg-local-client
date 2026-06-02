"""Config + token resolution for the generic single-bot Telegram client.

Non-secret config (bot slug, username, MCP name, the group chat_id(s) this agent
watches, and the name of the env var that holds the token) lives in a checked-in
JSON example and a gitignored local override. The token is the ONLY secret and is
resolved from the configured env var, or a `.env` file fallback, or a gitignored
token file — never from 1Password / op:// and never hardcoded.

Config keys (config.example.json → copy to config.local.json and fill):
  bot_slug         identifier for this bot (e.g. "glen"). Drives defaults.
  bot_username     the bot's Telegram @username, without the leading @ (optional;
                   informational — the live username is read from get_me()).
  mcp_name         MCP server name registered in ~/.claude.json.
                   Defaults to "<bot_slug>-tg".
  group_chat_ids   list of chat_ids this agent watches/sends to. The first entry
                   is the default target for send_message / get_tail_command. May
                   be empty until the agent has been added to a group.
  token_env_var    name of the env var that holds the bot token.
                   Defaults to "TG_BOT_TOKEN".
"""
import json
import os
from pathlib import Path
from typing import Optional

CLIENT_DIR = Path(__file__).resolve().parent.parent.parent  # client/
EXAMPLE_CONFIG_PATH = CLIENT_DIR / "config.example.json"

# TG_CONFIG_DIR separates per-project config from the shared code.  When set,
# config.local.json and .tg-bot-token live there instead of in CLIENT_DIR.
# This lets a plugin-cached code copy serve multiple projects simultaneously.
_config_dir_env = os.environ.get("TG_CONFIG_DIR")
CONFIG_DIR: Path = Path(_config_dir_env).expanduser().resolve() if _config_dir_env else CLIENT_DIR

LOCAL_CONFIG_PATH = CONFIG_DIR / "config.local.json"

# Gitignored token-file fallback when the configured env var is unset.
TOKEN_FILE = CONFIG_DIR / ".tg-bot-token"

# Built-in fallbacks if neither config file supplies them.
DEFAULT_TOKEN_ENV_VAR = "TG_BOT_TOKEN"


def _read_json(path: Path) -> dict:
    try:
        with path.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_config() -> dict:
    """Merge the checked-in example with the gitignored local override.

    Returns a dict with at least:
      bot_slug, bot_username, mcp_name, group_chat_ids (list), token_env_var.

    Derived defaults:
      * mcp_name defaults to "<bot_slug>-tg" when not set.
      * token_env_var defaults to "TG_BOT_TOKEN".
      * group_chat_ids is always a list (coerced from a bare int if needed).
    """
    cfg: dict = {
        "bot_slug": "",
        "bot_username": "",
        "mcp_name": "",
        "group_chat_ids": [],
        "token_env_var": DEFAULT_TOKEN_ENV_VAR,
    }
    for path in (EXAMPLE_CONFIG_PATH, LOCAL_CONFIG_PATH):
        data = _read_json(path)
        cfg.update({k: v for k, v in data.items() if v is not None})

    # Normalise group_chat_ids → list[int].
    raw = cfg.get("group_chat_ids", [])
    if isinstance(raw, int):
        raw = [raw]
    elif not isinstance(raw, list):
        raw = []
    cfg["group_chat_ids"] = [int(c) for c in raw]

    if not cfg.get("token_env_var"):
        cfg["token_env_var"] = DEFAULT_TOKEN_ENV_VAR
    if not cfg.get("mcp_name"):
        slug = cfg.get("bot_slug") or "tg-local"
        cfg["mcp_name"] = f"{slug}-tg"
    return cfg


def default_chat_id(cfg: Optional[dict] = None) -> Optional[int]:
    """The default send/watch target: the first configured group, or None."""
    cfg = cfg or load_config()
    chats = cfg.get("group_chat_ids") or []
    return chats[0] if chats else None


def token_env_var(cfg: Optional[dict] = None) -> str:
    """Name of the env var that holds the bot token (config-driven)."""
    cfg = cfg or load_config()
    return cfg.get("token_env_var") or DEFAULT_TOKEN_ENV_VAR


def _token_from_env_file(path: Path, var_name: str) -> Optional[str]:
    """Parse a .env-style file for a `<var_name>=...` line. Returns the value
    (quotes/whitespace stripped) or None. Tolerant of comments, blanks, `export `."""
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, val = line.partition("=")
        if key.strip() != var_name:
            continue
        val = val.strip().strip('"').strip("'").strip()
        if val:
            return val
    return None


def _search_dotenv(var_name: str) -> Optional[str]:
    """Find `<var_name>` in a .env file. Honour an explicit TG_ENV_FILE path first;
    otherwise walk UP from the client dir (the clone often lives inside the work
    repo, so the work `.env` is a parent) looking for the nearest .env that defines it."""
    explicit = os.environ.get("TG_ENV_FILE")
    if explicit:
        tok = _token_from_env_file(Path(explicit).expanduser(), var_name)
        if tok:
            return tok
    seen = set()
    for base in (CONFIG_DIR, *CONFIG_DIR.parents):
        if base in seen:
            continue
        seen.add(base)
        tok = _token_from_env_file(base / ".env", var_name)
        if tok:
            return tok
    return None


def resolve_token(cfg: Optional[dict] = None) -> Optional[str]:
    """Resolve the bot token, in order:
      1. the configured env var (default TG_BOT_TOKEN) — if actually set, not the
         unsubstituted ${...} literal,
      2. a .env file: TG_ENV_FILE if set, else the nearest .env walking up from the
         client dir (so a token in the parent work repo's gitignored .env is found),
      3. the gitignored .tg-bot-token file in the client dir.
    Whitespace is stripped throughout. Returns None if nothing is found."""
    cfg = cfg or load_config()
    var_name = token_env_var(cfg)

    env = os.environ.get(var_name)
    if env and env.strip() and not env.strip().startswith("${"):
        return env.strip()
    dotenv = _search_dotenv(var_name)
    if dotenv:
        return dotenv
    try:
        text = TOKEN_FILE.read_text().strip()
        return text or None
    except FileNotFoundError:
        return None
