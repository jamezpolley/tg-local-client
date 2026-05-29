"""Bootstrap: MCP-entry shape (config-driven), registration idempotency + backup,
and config.local.json creation from the example."""
import glob
import json
from pathlib import Path

import tg_local_client.bootstrap as bootstrap
import tg_local_client.config as config


def test_build_mcp_entry_shape_default_var():
    entry = bootstrap.build_mcp_entry(Path("/home/agent/repo/client"),
                                      mcp_name="glen-tg", var_name="TG_BOT_TOKEN")
    assert entry["command"] == "uv"
    assert entry["args"] == ["run", "--directory", "/home/agent/repo/client",
                             "tg-local-mcp"]
    # Token passed through env as an unsubstituted ${...} ref, never a literal value.
    assert entry["env"]["TG_BOT_TOKEN"] == "${TG_BOT_TOKEN}"


def test_build_mcp_entry_shape_custom_var():
    entry = bootstrap.build_mcp_entry(Path("/x/client"), mcp_name="acme-tg",
                                      var_name="ACME_BOT_TOKEN")
    assert entry["env"] == {"ACME_BOT_TOKEN": "${ACME_BOT_TOKEN}"}


def _point_config(tmp_path, monkeypatch, local):
    ex = tmp_path / "config.example.json"
    lo = tmp_path / "config.local.json"
    ex.write_text(json.dumps({}))
    lo.write_text(json.dumps(local))
    monkeypatch.setattr(config, "EXAMPLE_CONFIG_PATH", ex)
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", lo)
    monkeypatch.setattr(bootstrap, "EXAMPLE_CONFIG_PATH", ex)
    monkeypatch.setattr(bootstrap, "LOCAL_CONFIG_PATH", lo)


def test_register_mcp_creates_when_absent(tmp_path, monkeypatch):
    _point_config(tmp_path, monkeypatch, {"bot_slug": "glen"})
    cfg = tmp_path / ".claude.json"
    status = bootstrap.register_mcp(config_path=cfg, client_dir=Path("/x/client"))
    assert status == "created"
    data = json.loads(cfg.read_text())
    assert "glen-tg" in data["mcpServers"]  # derived mcp_name


def test_register_mcp_uses_custom_mcp_name(tmp_path, monkeypatch):
    _point_config(tmp_path, monkeypatch,
                  {"bot_slug": "glen", "mcp_name": "weird-name",
                   "token_env_var": "ZBOT"})
    cfg = tmp_path / ".claude.json"
    status = bootstrap.register_mcp(config_path=cfg, client_dir=Path("/x/client"))
    assert status == "created"
    data = json.loads(cfg.read_text())
    assert "weird-name" in data["mcpServers"]
    assert data["mcpServers"]["weird-name"]["env"] == {"ZBOT": "${ZBOT}"}


def test_register_mcp_idempotent_and_backs_up(tmp_path, monkeypatch):
    _point_config(tmp_path, monkeypatch, {"bot_slug": "glen"})
    cfg = tmp_path / ".claude.json"
    # Pre-existing config with an unrelated server — must be preserved + backed up.
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    status1 = bootstrap.register_mcp(config_path=cfg, client_dir=Path("/x/client"))
    assert status1 == "added"
    data = json.loads(cfg.read_text())
    assert "other" in data["mcpServers"]  # preserved
    assert "glen-tg" in data["mcpServers"]
    # A backup was made.
    backups = glob.glob(str(cfg) + ".bak.*")
    assert backups, "expected a backup of the pre-existing config"

    # Second run is a no-op insert.
    status2 = bootstrap.register_mcp(config_path=cfg, client_dir=Path("/x/client"))
    assert status2 == "already-present"


def test_write_local_config_creates_from_example(tmp_path, monkeypatch):
    ex = tmp_path / "config.example.json"
    lo = tmp_path / "config.local.json"
    ex.write_text(json.dumps({"bot_slug": "", "token_env_var": "TG_BOT_TOKEN"}))
    monkeypatch.setattr(bootstrap, "EXAMPLE_CONFIG_PATH", ex)
    monkeypatch.setattr(bootstrap, "LOCAL_CONFIG_PATH", lo)

    assert bootstrap.write_local_config() is True
    assert lo.exists()
    assert json.loads(lo.read_text())["token_env_var"] == "TG_BOT_TOKEN"
    # Second call is a no-op.
    assert bootstrap.write_local_config() is False


def test_main_refuses_to_register_with_empty_slug(tmp_path, monkeypatch, capsys):
    """An empty bot_slug would collide on the data dir + mcp_name; bootstrap must
    stop after config creation and tell the user to set the slug, without touching
    ~/.claude.json."""
    _point_config(tmp_path, monkeypatch, {"bot_slug": "", "group_chat_ids": []})
    claude_cfg = tmp_path / ".claude.json"
    monkeypatch.setattr(bootstrap, "CLAUDE_CONFIG_PATH", claude_cfg)

    called = {"register": False}
    monkeypatch.setattr(
        bootstrap, "register_mcp",
        lambda *a, **k: called.__setitem__("register", True) or "added")

    rc = bootstrap.main()
    assert rc == 0
    assert called["register"] is False  # never registered with a blank slug
    assert not claude_cfg.exists()
    assert "bot_slug is empty" in capsys.readouterr().out
