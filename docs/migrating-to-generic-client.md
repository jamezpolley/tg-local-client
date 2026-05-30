# Migrating to the generic `tg-local-client`

For Glen's agent (and any agent moving off a bespoke per-agent Telegram client onto
the shared generic one).

Your Telegram client is moving from the bespoke **`glen-tg-client`** to the shared,
generic **`tg-local-client`** — the same client the other fleet agents now run. It
just reached full feature parity:

- 21 tools — send / edit / delete message, `send_typing`, `react_to_message`,
  `reply_to_message_id` threading (+ inbound reply-to surfaced), `send_photo` /
  `send_document`, `download_media`, `list_known_chats`, identity/trust lookups,
  forum-topic create/edit/close/reopen, `client_version`.
- Optional Haiku **`--triage`** ACT/SKIP layer on `tg-local-tail` (with
  `--wake-on` / `--channel-topics` / self-exclude deterministic filters).

**Why:** one shared client to maintain instead of N bespoke ones — you pick up new
tools automatically as the generic client improves.

## Switch steps

1. **Accept the GitHub invite** to `jamezpolley/tg-local-client` (read access), then clone:
   ```
   gh repo clone jamezpolley/tg-local-client && cd tg-local-client
   ```
2. **Configure** — copy `config.example.json` → `config.local.json` and fill with your
   existing settings (nothing new to obtain):
   - `bot_slug`: `"glen"`
   - `bot_username`: your existing bot's username (unchanged)
   - `group_chat_ids`: your existing group(s)
   - `token_env_var`: `"TG_BOT_TOKEN"` (or whatever you used) — your token stays in
     your gitignored `.env`, **unchanged**.
3. **Bootstrap**: `uv run tg-local-bootstrap` (installs deps, registers the MCP in
   `~/.claude.json`, posts a hello to your group).
4. **Restart your Claude session** so the new MCP loads its tools.
5. *(Optional)* point your monitor at the built-in `tg-local-tail --triage` if you
   want the Haiku ACT/SKIP filter instead of a hand-rolled one.

## After you're across

Confirm the generic client works (send a test message, check `client_version` shows
21 tools), then tell James — he'll retire `glen-tg-client` and revoke its old invite.

**Security note:** `config.local.json` and `.env` are gitignored — your token never
goes near the repo, the group, or any log.
