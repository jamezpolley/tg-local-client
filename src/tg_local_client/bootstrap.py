"""Idempotent one-shot setup for the generic local Telegram client.

Run once after cloning + filling config.local.json + setting the token:

    uv run tg-local-bootstrap

It will:
  (a) ensure deps (uv already provisioned them by running this),
  (b) copy config.example.json → config.local.json if absent (and tell you to fill it),
  (c) register this client's MCP in your Claude config (~/.claude.json) under the
      configured mcp_name, backing it up first, skip-if-already-present,
  (d) if the token AND at least one group_chat_id are present, post a hello + a
      trust-binding request into each configured group,
  (e) if the token or a group is missing, print the precise next step and exit 0.

Re-running is safe: every step is skip-if-done.
"""
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

from .config import (CLIENT_DIR, CONFIG_DIR, EXAMPLE_CONFIG_PATH,
                     LOCAL_CONFIG_PATH, load_config, resolve_token,
                     token_env_var)

CLAUDE_CONFIG_PATH = Path.home() / ".claude.json"


def build_mcp_entry(client_dir: Path, mcp_name: str, var_name: str) -> dict:
    """The exact stdio MCP server entry to inject into the Claude config.

    command: `uv run --directory <client-dir> tg-local-mcp`
    The token env var (e.g. TG_BOT_TOKEN) is passed THROUGH from the environment as
    an unsubstituted "${VAR}" reference — NOT written as a literal value — so the
    secret never lands in ~/.claude.json.
    """
    env: dict = {var_name: "${" + var_name + "}"}
    if CONFIG_DIR != CLIENT_DIR:
        env["TG_CONFIG_DIR"] = str(CONFIG_DIR)
    return {
        "command": "uv",
        "args": ["run", "--directory", str(client_dir), "tg-local-mcp"],
        "env": env,
    }


def write_local_config() -> bool:
    """Create config.local.json from the checked-in example if it doesn't exist.

    Returns True if a file was written, False if it already existed.
    """
    if LOCAL_CONFIG_PATH.exists():
        return False
    try:
        example = json.loads(EXAMPLE_CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        example = {}
    LOCAL_CONFIG_PATH.write_text(json.dumps(example, indent=2) + "\n")
    return True


def register_mcp(config_path: Path = CLAUDE_CONFIG_PATH,
                 client_dir: Path = CLIENT_DIR,
                 mcp_name: str = None,
                 var_name: str = None) -> str:
    """Append this client's stdio MCP entry to the Claude config, idempotently.

    Backs up the existing config (once per invocation) before writing. Returns one
    of: "added", "already-present", "created". mcp_name / var_name default to the
    loaded config when not passed (kept as args so tests can pin them)."""
    cfg = load_config()
    mcp_name = mcp_name or cfg["mcp_name"]
    var_name = var_name or token_env_var(cfg)
    entry = build_mcp_entry(client_dir, mcp_name, var_name)

    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            data = {}
        # Back up before mutating.
        backup = config_path.with_suffix(
            config_path.suffix + f".bak.{int(time.time())}")
        shutil.copy2(config_path, backup)
        status = "added"
    else:
        data = {}
        status = "created"

    if not isinstance(data, dict):
        data = {}
    servers = data.setdefault("mcpServers", {})
    if mcp_name in servers:
        return "already-present"

    servers[mcp_name] = entry
    config_path.write_text(json.dumps(data, indent=2) + "\n")
    return status


async def _post_hello(cfg: dict, token: str) -> None:
    """Post a hello + trust-binding request into each configured group."""
    from aiogram import Bot

    slug = cfg.get("bot_slug") or "this agent"
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        for chat_id in cfg["group_chat_ids"]:
            await bot.send_message(
                chat_id=chat_id,
                text=(f"👋 Hello from {slug}'s agent (@{me.username}). I'm online and "
                      "watching this group. Please add the trust binding for my bot "
                      f"id {me.id} and my human user_id on your side so your agents "
                      f"can resolve me as `{slug}`. I'll treat this group as "
                      "collaboration, not commands."),
            )
            print(f"  posted hello as @{me.username} (id={me.id}) to chat {chat_id}")
    finally:
        await bot.session.close()


def main() -> int:
    print("tg-local-client bootstrap")

    cfg = load_config()
    var_name = token_env_var(cfg)

    wrote = write_local_config()
    if wrote:
        print(f"  config.local.json: written from config.example.json")
        print(f"  → EDIT {LOCAL_CONFIG_PATH} now: set bot_slug, bot_username, and")
        print( "    group_chat_ids (the chat_id of the group you were added to).")
        # Reload so subsequent steps see any inline edits if re-run; for a fresh
        # write the values are still placeholders, so fall through to the guidance.
        cfg = load_config()
        var_name = token_env_var(cfg)
    else:
        print("  config.local.json: already present")

    # Guard: an empty bot_slug poisons BOTH the per-slug data dir (falls back to
    # "default") AND the derived mcp_name ("tg-local-tg"). Two unconfigured bots
    # would then silently collide on the data store and on the ~/.claude.json MCP
    # entry (the second register_mcp would see the name present and keep the FIRST
    # bot's command). Refuse to register until the slug is set.
    if not (cfg.get("bot_slug") or "").strip():
        print()
        print(f"  bot_slug is empty. Set it in {LOCAL_CONFIG_PATH} before continuing —")
        print( "  it drives your data dir (~/.local/share/tg-local/<slug>/) and your")
        print( "  MCP name (<slug>-tg). Leaving it blank would collide with any other")
        print( "  unconfigured client on this machine. Then re-run bootstrap.")
        print()
        return 0

    status = register_mcp(mcp_name=cfg["mcp_name"], var_name=var_name)
    print(f"  MCP '{cfg['mcp_name']}' registration in {CLAUDE_CONFIG_PATH}: {status}")

    token = resolve_token(cfg)
    groups = cfg.get("group_chat_ids") or []

    if not token:
        print()
        print(f"  No bot token found ({var_name} unset). It's the only secret you handle.")
        print( "  Ask the human who set you up for the token — it's passed to you")
        print( "  out-of-band, never committed. Then add it to your project's")
        print( "  gitignored .env (or export it):")
        print()
        print(f"      {var_name}=<the token you were given>")
        print()
        print( "  The client finds it by searching up from its own directory for a")
        print(f"  .env (your work repo's .env is a parent), or set TG_ENV_FILE to")
        print( "  point at it. Then re-run bootstrap, or restart your Claude session.")
        print()
        return 0

    print("  token: present")

    if not groups:
        print()
        print( "  No group_chat_ids configured yet. Ask the human who set you up which")
        print(f"  group(s) you belong to, add their chat_id(s) to group_chat_ids in")
        print(f"  {LOCAL_CONFIG_PATH}, then re-run bootstrap to post your hello.")
        print()
        return 0

    try:
        asyncio.run(_post_hello(cfg, token))
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: could not post hello ({type(exc).__name__}: {exc}).")
        print("  Check the token is correct and the bot is a member of the group(s).")
        return 1

    print()
    print(f"  Done. Restart your Claude session to load the '{cfg['mcp_name']}' MCP,")
    print( "  then your agent can send_message / get_tail_command in the group(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
