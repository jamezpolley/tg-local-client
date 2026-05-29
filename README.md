# Local Telegram client (generic)

A tiny, self-contained Telegram client that gives an agent its own local Telegram
connection — one bot, its own token, its own group(s). It's a single process: an
MCP server that owns one bot token, long-polls Telegram for new messages, and gives
your Claude agent a small set of tools to send and watch messages.

Clone it, fill in a config file, drop the token where the config points, run one
command. Nothing else is hardcoded — any agent on any machine can stand up its own
connection from this same repo.

**The bot token is the only secret you handle.** No API keys, no 1Password, no
config to hand-edit beyond the one config file.

## Setup (4 steps)

1. **Clone** this repo:
   ```bash
   git clone <repo-url> tg-local-client
   cd tg-local-client
   ```

2. **Fill in `config.local.json`.** Copy the example and edit it (the bootstrap will
   also create it for you on first run, but you'll need to edit it either way):
   ```bash
   cp config.example.json config.local.json
   ```
   Fields:

   | Field            | Meaning                                                              | Default            |
   |------------------|----------------------------------------------------------------------|--------------------|
   | `bot_slug`       | Short identifier for this bot, e.g. `"glen"`. Drives other defaults. | _(required)_       |
   | `bot_username`   | The bot's Telegram @username (without the `@`). Informational.       | `""`               |
   | `mcp_name`       | The MCP server name registered in `~/.claude.json`.                  | `"<bot_slug>-tg"`  |
   | `group_chat_ids` | List of chat_ids this agent watches/sends to. First = default.       | `[]`               |
   | `token_env_var`  | Name of the env var holding the bot token.                           | `"TG_BOT_TOKEN"`   |

   **Ask the human who set you up which group(s) you belong to** and put their
   chat_id(s) in `group_chat_ids`. It can stay empty until you've been added to a
   group — bootstrap will tell you what's still missing.

3. **Put the token where the config points.** Ask the human who set you up for the
   token (it's passed to you out-of-band). Easiest: add it to your work project's
   **gitignored `.env`** under whatever name `token_env_var` is (default
   `TG_BOT_TOKEN`):
   ```
   TG_BOT_TOKEN=<the token you were given>
   ```
   The client finds it automatically — it searches up from its own directory for a
   `.env` (since this clone usually lives *inside* your work repo, that `.env` is a
   parent). If your `.env` lives somewhere unusual, set `TG_ENV_FILE=/path/to/.env`.
   An `export TG_BOT_TOKEN=…` in your shell, or a gitignored `.tg-bot-token` file in
   this repo, also work. **Never** put the token in a tracked file.

4. **Run bootstrap** once, from the repo root:
   ```bash
   uv run tg-local-bootstrap
   ```

That's it: `clone` → fill `config.local.json` → put the token where config points →
one `uv run` command.

Bootstrap will install dependencies (via `uv`), create `config.local.json` from the
example if missing, register the MCP server (under your `mcp_name`) in your Claude
config (`~/.claude.json`, backed up first), and — if a token and at least one group
are configured — post a hello into the group(s). Restart your Claude session
afterwards so it loads the MCP. Re-running bootstrap is always safe.

## What you get

Your agent gets four tools from the MCP server (named by `mcp_name`):

- `send_message(text)` — send a message into your group (first configured group).
- `list_recent_messages()` — read recent inbound messages from the local store.
- `get_tail_command()` — get a flat command to watch the group live (run it in the
  background, then Monitor it).
- `mark_read(message_ids)` — mark messages read locally.

While your Claude session is open, the client is polling Telegram. When you close
it, polling stops — that's fine. Telegram holds the conversation; you'll catch up on
the next inbound when you're back.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — the only thing you need installed. It
  provisions Python and all dependencies automatically. No `mise`, no `pip`.

## Notes

- **Gitignore this checkout in your parent repo.** If you cloned this inside your
  work project, add `tg-local-client/` to that project's `.gitignore` (or
  `.git/info/exclude`) so the client never gets committed into your project.
- Don't commit the token. `.tg-bot-token`, `*.token`, `.env*`, and
  `config.local.json` are gitignored.
- Local message state lives outside the repo, under
  `~/.local/share/tg-local/<bot_slug>/` (override with `TG_LOCAL_DATA`), so two bots
  on one machine never collide.
- If the token ever leaks, tell the human who runs the other side — they rotate it
  and hand you a new one. Update your token and restart.

See `AGENTS.md` for the conventions your agent should follow in the group.
