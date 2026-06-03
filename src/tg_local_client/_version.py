"""Client version fingerprint.

CLIENT_VERSION is a constant compiled into the module at import time, so the
RUNNING MCP process reports the version of the code it actually loaded — not
whatever is currently on disk.

## Bump convention

Format: "YYYY-MM-DD.N" where YYYY-MM-DD is the date of the change and N is a
1-based sequence within that day (reset to 1 each new day).

EVERY change that adds/modifies an MCP tool or observable behaviour MUST bump
CLIENT_VERSION. Bumping it is how the `client_version` tool stays meaningful as
a staleness signal.
"""

CLIENT_VERSION = "2026-06-03.2"
