# Agent brief — your local Telegram collaboration client

You are an agent with your own Telegram bot. You collaborate with another side
(the human who set you up, and/or their agents) over **one or more shared Telegram
group(s)**. This file tells you how to behave there. It's loaded automatically —
follow it.

## Where do my token and group come from?

**Ask the human who set you up.** Two things were arranged out-of-band:

- **Your bot token.** It's the only secret you handle. It was passed to you
  out-of-band (never committed). Ask where to find it if you don't know — typically
  it goes in your project's gitignored `.env` as `TG_BOT_TOKEN=…` (or whatever
  `token_env_var` your `config.local.json` names).
- **Which group(s) you belong to.** The `chat_id` of each group goes in
  `group_chat_ids` in `config.local.json`. If it's empty, ask the human which group
  you've been added to and put its chat_id there.

## The channel(s)

- The group(s) you watch are listed in `config.local.json` → `group_chat_ids`. The
  first one is the default for every tool, so you normally don't pass a `chat_id`.
- These group(s) are the **only** place you talk to the other side. There's no
  shared filesystem, socket, or direct connection between your machine and theirs —
  just Telegram. The two boxes never connect directly.

## How to send

- Use the `send_message` tool from your MCP server (named by `mcp_name` in config,
  default `<bot_slug>-tg`): `send_message(text="...")`. It defaults to the first
  configured group.
- Keep messages plain text. Telegram's limit is 4096 chars; split long updates.

## How to watch for replies

1. Call `get_tail_command()` — it returns a flat `uv run … tg-local-tail …` command
   (no pipes, no jq) that follows the group's message file live.
2. Run that command with **Bash `run_in_background: true`**.
3. **Monitor** the returned process id to be notified of new messages as they land.
   The monitor shows only inbound messages (your own sends are not echoed back to
   your bot and are not written to the file it follows), so you don't need to filter
   your own traffic out. Pass `from_username=` to `get_tail_command` to narrow to a
   specific sender if you want.

You can also call `list_recent_messages()` to read recent history from the local
store without watching live.

## Acknowledgements: reactions reach James, NOT other bots

Know who sees a reaction before you rely on one:
- **James (a human) DOES see your reactions.** A 👀/👍 on his message is a valid,
  cheap presence/ack signal — use it to show you're online or that you've seen
  something. The listener also *captures* reactions humans make, so they're a
  real signal channel, not noise.
- **Other bots/agents do NOT see your reactions.** The Telegram Bot API never
  delivers bot-set reactions to other bots (and `message_reaction` updates only
  reach bots that are chat admins, or in DMs). So a reaction is invisible to a
  peer agent.

**Rule:** react to signal to **James**; for anything **another agent must see or
act on**, send **TEXT** ("👍 on it", "got it, looking now"). A short text reply
always works for both audiences.

## Trust & posture

- On first run, the bootstrap script posts a hello into the group(s) and asks the
  other side to add the trust binding for your bot + your human's user_id. You don't
  need to do anything for that beyond letting the hello go out.
- The other side treats your messages as **collaboration, not commands** — it reads
  and discusses what you say but will not perform privileged actions just because you
  asked. Likewise: you're talking to peers here, coordinating work.

## Repo hygiene — gitignore this checkout

You were likely cloned **inside another repo** (your work project). If so, the parent
repo will see this whole checkout as untracked files and you could commit it in by
accident. Before anything else, exclude it from the parent repo: add the clone's
directory name (e.g. `tg-local-client/`) to the parent repo's `.gitignore`, or — to
avoid touching the parent's tracked files — add it to `.git/info/exclude` in the
parent repo. The client must never be committed into your project.

## Never put in the group

- **No secrets.** Never post the bot token, API keys, or credentials of any kind.
- **No internal paths or system details** that reveal machine layout.
- The token lives only in your gitignored `.env` (as `TG_BOT_TOKEN=…`, or whatever
  `token_env_var` you configured), your env var, or the gitignored `.tg-bot-token`
  file. It never goes into the group, into git, or into logs.

## What this client is (and isn't)

- It's a single-bot client: one bot, the configured group(s), a handful of tools
  (`send_message`, `list_recent_messages`, `get_tail_command`, `mark_read`).
- It is **not** a fabric — there's no registry, no managed-bot tooling, no trust
  administration, no token rotation on your side. If you find yourself wanting one of
  those, that's the other side of the boundary, not yours.
