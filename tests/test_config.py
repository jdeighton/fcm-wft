import re
from pathlib import Path

import pytest

from wft.config import (REPO_ROOT, SUPPORTED_KEYS, Config, ConfigError, check_environment, check_share_marker,
                        load_config, load_rules, parse_env_file)

RULES = {
    "prod": {"WFT_SHARE_ROOT": [r"[\\/]prod([\\/]|$)"]},
    "uat": {"WFT_SHARE_ROOT": [r"[\\/]uat([\\/]|$)"]},
}


def test_parse_env_file_handles_comments_and_quotes():
    assert parse_env_file('# c\nA=1\nB="x y"\n\nC=\\\\srv\\share\n') == {"A": "1", "B": "x y", "C": "\\\\srv\\share"}


def test_load_config(tmp_path):
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/mnt/share/uat\nWFT_PORT=9000\n")
    cfg = load_config("uat", home=tmp_path)
    assert str(cfg.share_root) == "/mnt/share/uat" and cfg.port == 9000
    assert cfg.cache_path.parent == tmp_path / "cache" and cfg.cache_path.name.startswith("uat-")


def test_cache_is_per_share(tmp_path):
    def cfg(share):
        return Config(env="uat", share_root=Path(share), port=1, home=tmp_path, definitions_dir=tmp_path)
    assert cfg("/s/a/uat").cache_path != cfg("/s/b/uat").cache_path
    assert cfg("/s/a/uat").cache_path == cfg("/s/a/uat").cache_path


def test_missing_env_file_and_unknown_env(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config("prod", home=tmp_path)
    with pytest.raises(ConfigError, match="Unknown environment"):
        load_config("dev", home=tmp_path)


def test_share_root_required(tmp_path):
    (tmp_path / "uat.env").write_text("WFT_PORT=1\n")
    with pytest.raises(ConfigError, match="WFT_SHARE_ROOT"):
        load_config("uat", home=tmp_path, rules={})


def test_unknown_keys_rejected(tmp_path):
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/s/uat\nWFT_SHARE_ROT=typo\n")
    with pytest.raises(ConfigError, match="WFT_SHARE_ROT"):
        load_config("uat", home=tmp_path)


@pytest.mark.parametrize("env, share, problem", [
    ("uat", r"\\srv\wft\prod", "looks like a prod value"),
    ("uat", r"\\srv\wft\other", "does not look like a uat value"),
    ("prod", r"\\srv\wft\uat", "looks like a uat value"),
    ("prod", "/mnt/wft/production-ish", "does not look like a prod value"),
])
def test_mixed_environments_refused(env, share, problem):
    with pytest.raises(ConfigError, match=re.escape(problem)):
        check_environment(env, {"WFT_SHARE_ROOT": share}, RULES)


@pytest.mark.parametrize("env, share", [("uat", r"\\srv\wft\UAT"), ("prod", "/mnt/wft/prod/")])
def test_matching_environment_accepted(env, share):
    check_environment(env, {"WFT_SHARE_ROOT": share, "WFT_PORT": "1"}, RULES)


def test_repo_rules_load_and_cover_every_environment():
    rules = load_rules()
    assert set(rules) == {"prod", "uat"}
    assert set().union(*(r.keys() for r in rules.values())) <= set(SUPPORTED_KEYS)


def test_share_marker(tmp_path):
    cfg = Config(env="uat", share_root=tmp_path, port=1, home=tmp_path, definitions_dir=tmp_path)
    with pytest.raises(ConfigError, match="missing"):
        check_share_marker(cfg)
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "environment").write_text("prod\n")
    with pytest.raises(ConfigError, match="marked 'prod'"):
        check_share_marker(cfg)
    (tmp_path / "meta" / "environment").write_text("UAT\n")
    check_share_marker(cfg)


def test_env_example_lists_every_supported_key():
    text = (REPO_ROOT / ".env.example").read_text()
    keys = set(re.findall(r"^#?\s*(WFT_[A-Z_]+)=", text, re.MULTILINE))
    assert keys == set(SUPPORTED_KEYS)
