import pytest

from wft.config import ConfigError, load_config, parse_env_file


def test_parse_env_file_handles_comments_and_quotes():
    assert parse_env_file('# c\nA=1\nB="x y"\n\nC=\\\\srv\\share\n') == {"A": "1", "B": "x y", "C": "\\\\srv\\share"}


def test_load_config(tmp_path):
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/mnt/share/uat\nWFT_PORT=9000\n")
    cfg = load_config("uat", home=tmp_path)
    assert str(cfg.share_root) == "/mnt/share/uat" and cfg.port == 9000
    assert cfg.cache_path == tmp_path / "cache" / "uat.sqlite"


def test_missing_env_file_and_unknown_env(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config("prod", home=tmp_path)
    with pytest.raises(ConfigError, match="Unknown environment"):
        load_config("dev", home=tmp_path)


def test_share_root_required(tmp_path):
    (tmp_path / "uat.env").write_text("WFT_PORT=1\n")
    with pytest.raises(ConfigError, match="WFT_SHARE_ROOT"):
        load_config("uat", home=tmp_path)
