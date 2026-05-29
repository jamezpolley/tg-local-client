"""Generic single-bot Telegram client for a local agent.

One process: a stdio MCP server that owns one bot token, long-polls Telegram, and
exposes a small tool surface. No fabric, no registry, no control socket, no
1Password, no systemd. The bot token is the only secret.

Everything bot-specific (slug, username, MCP name, which group(s) to watch, which
env var holds the token) lives in `config.local.json` — clone, fill it in, drop the
token where the config points, and run the bootstrap. See README.md / AGENTS.md.
"""
