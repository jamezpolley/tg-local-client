"""Config load/merge/defaults + token resolution (env, .env, ${...} guard, token file)."""
import json
from pathlib import Path

import tg_local_client.config as config


def _write_cfg(tmp_path, monkeypatch, example=None, local=None):
    """Point config at temp example/local files. Returns nothing; sets module paths."""
    ex = tmp_path / "config.example.json"
    lo = tmp_path / "config.local.json"
    ex.write_text(json.dumps(example if example is not None else {}))
    if local is not None:
        lo.write_text(json.dumps(local))
    monkeypatch.setattr(config, "EXAMPLE_CONFIG_PATH", ex)
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", lo)
    return ex, lo


# ---- config load / merge / defaults ----

def test_load_config_defaults_mcp_name_from_slug(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "glen"})
    cfg = config.load_config()
    assert cfg["bot_slug"] == "glen"
    assert cfg["mcp_name"] == "glen-tg"           # derived
    assert cfg["token_env_var"] == "TG_BOT_TOKEN"  # default
    assert cfg["group_chat_ids"] == []


def test_load_config_local_overrides_example(tmp_path, monkeypatch):
    _write_cfg(
        tmp_path, monkeypatch,
        example={"bot_slug": "x", "token_env_var": "TG_BOT_TOKEN"},
        local={"bot_slug": "real", "mcp_name": "custom-name",
               "group_chat_ids": [-100, -200], "token_env_var": "REAL_TOKEN"},
    )
    cfg = config.load_config()
    assert cfg["bot_slug"] == "real"
    assert cfg["mcp_name"] == "custom-name"        # explicit wins over derived
    assert cfg["group_chat_ids"] == [-100, -200]
    assert cfg["token_env_var"] == "REAL_TOKEN"


def test_group_chat_ids_coerces_bare_int_to_list(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g", "group_chat_ids": -123})
    cfg = config.load_config()
    assert cfg["group_chat_ids"] == [-123]


def test_default_chat_id_and_empty(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g", "group_chat_ids": [-9, -8]})
    assert config.default_chat_id() == -9
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g"})
    assert config.default_chat_id() is None


# ---- token resolution ----

def test_resolve_token_from_env_default_var(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g"})
    monkeypatch.setenv("TG_BOT_TOKEN", "  tok-from-env  ")
    assert config.resolve_token() == "tok-from-env"


def test_resolve_token_from_configured_var(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch,
               local={"bot_slug": "g", "token_env_var": "MY_BOT_TOKEN"})
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.setenv("MY_BOT_TOKEN", "tok-custom")
    assert config.resolve_token() == "tok-custom"


def test_unsubstituted_template_literal_is_ignored(tmp_path, monkeypatch):
    """A `${TG_BOT_TOKEN}` literal (unsubstituted ${...}) must NOT be treated as the
    token; resolution should fall through to .env / token file."""
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g"})
    monkeypatch.setenv("TG_BOT_TOKEN", "${TG_BOT_TOKEN}")
    monkeypatch.delenv("TG_ENV_FILE", raising=False)
    # Neutralise the upward .env walk so the test is deterministic regardless of any
    # real .env above the package dir; then assert the token-file fallback is used.
    monkeypatch.setattr(config, "_search_dotenv", lambda var_name: None)
    tf = tmp_path / ".tg-bot-token"
    tf.write_text("file-token\n")
    monkeypatch.setattr(config, "TOKEN_FILE", tf)
    assert config.resolve_token() == "file-token"


def test_resolve_token_from_env_file_via_tg_env_file(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g"})
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    envf = tmp_path / "secrets.env"
    envf.write_text("# comment\nexport TG_BOT_TOKEN='dotenv-tok'\nOTHER=1\n")
    monkeypatch.setenv("TG_ENV_FILE", str(envf))
    assert config.resolve_token() == "dotenv-tok"


def test_resolve_token_env_file_honours_configured_var_name(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch,
               local={"bot_slug": "g", "token_env_var": "WEIRD_TOKEN"})
    monkeypatch.delenv("WEIRD_TOKEN", raising=False)
    envf = tmp_path / "x.env"
    # The .env defines a DIFFERENT var too; only the configured one matches.
    envf.write_text("TG_BOT_TOKEN=wrong\nWEIRD_TOKEN=right-one\n")
    monkeypatch.setenv("TG_ENV_FILE", str(envf))
    assert config.resolve_token() == "right-one"


def test_resolve_token_from_token_file_last(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g"})
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_ENV_FILE", raising=False)
    monkeypatch.setattr(config, "_search_dotenv", lambda var_name: None)
    tf = tmp_path / ".tg-bot-token"
    tf.write_text("  filetok  \n")
    monkeypatch.setattr(config, "TOKEN_FILE", tf)
    assert config.resolve_token() == "filetok"


def test_resolve_token_none_when_nothing_set(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, local={"bot_slug": "g"})
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_ENV_FILE", raising=False)
    monkeypatch.setattr(config, "_search_dotenv", lambda var_name: None)
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "nope")
    assert config.resolve_token() is None
