"""Firm-specific environment-check patterns live in a local file, merged over the public defaults (#38)."""
import re

import pytest
import yaml

from wft.config import (ConfigError, check_environment, effective_rules, load_config, load_rules, merge_rules,
                        rules_errors)

# Made-up values only: the repo is public.
UAT_SHARE = r"\\files.example.invalid\wft\uat"
PROD_SHARE = r"\\files.example.invalid\wft\prod"
LOCAL = {
    "prod": {"WFT_TT_BASE_URL": [r"^https://tt-prod\.example\.invalid"]},
    "uat": {"WFT_TT_BASE_URL": [r"^https://tt-uat\.example\.invalid"]},
}


def env_file(home, **values):
    base = {"WFT_SHARE_ROOT": UAT_SHARE}
    base.update(values)
    (home / "uat.env").write_text("".join(f"{k}={v}\n" for k, v in base.items()))


def tt(url):
    return {"WFT_TT_BASE_URL": url, "WFT_TT_ENVIRONMENT": "ext_uat_cert", "WFT_TT_APP_KEY": "k",
            "WFT_TT_APP_SECRET": "super-secret-value"}


def test_repo_defaults_are_generic():
    rules = load_rules()
    text = yaml.safe_dump(rules)
    assert set(rules) == {"prod", "uat"} and rules_errors(rules) == []
    assert not re.search(r"\.(com|net|org|local|corp|internal|tt)\b|\\\\[a-z]", text, re.IGNORECASE), text


def test_merge_is_per_environment_and_key():
    base = {"prod": {"A": ["p"], "B": ["pb"]}, "uat": {"A": ["u"]}}
    merged = merge_rules(base, {"prod": {"A": ["local-p"]}, "uat": {"C": ["uc"]}})
    assert merged == {"prod": {"A": ["local-p"], "B": ["pb"]}, "uat": {"A": ["u"], "C": ["uc"]}}
    assert base["prod"]["A"] == ["p"]  # defaults untouched


def test_no_local_file_means_todays_behaviour(tmp_path):
    assert effective_rules(tmp_path) == load_rules()
    env_file(tmp_path, **tt("https://tt-prod.example.invalid"))
    assert load_config("uat", home=tmp_path).tt.base_url == "https://tt-prod.example.invalid"  # generic rules can't tell


def test_local_pattern_catches_a_uat_profile_pointed_at_production(tmp_path):
    (tmp_path / "environments.yaml").write_text(yaml.safe_dump(LOCAL))
    env_file(tmp_path, **tt("https://tt-prod.example.invalid/api"))
    with pytest.raises(ConfigError) as exc:
        load_config("uat", home=tmp_path)
    message = str(exc.value)
    assert "WFT_TT_BASE_URL=https://tt-prod.example.invalid/api does not look like a uat value" in message
    assert "looks like a prod value" in message
    assert "super-secret-value" not in message
    env_file(tmp_path, **tt("https://tt-uat.example.invalid/api"))
    assert load_config("uat", home=tmp_path).tt.base_url == "https://tt-uat.example.invalid/api"


def test_local_file_keeps_the_generic_share_check(tmp_path):
    (tmp_path / "environments.yaml").write_text(yaml.safe_dump(LOCAL))
    env_file(tmp_path, WFT_SHARE_ROOT=PROD_SHARE)
    with pytest.raises(ConfigError, match="looks like a prod value"):
        load_config("uat", home=tmp_path)


@pytest.mark.parametrize("content, problem", [
    ("prod: [unclosed", "isn't valid YAML"),
    ("- just a list", "must map each environment"),
    ("staging: {}", "unknown environment 'staging'"),
    ("uat: {WFT_SHARE_ROOT: '[\\\\/]uat'}", "must be a list of patterns"),
    ("uat: {WFT_NOPE: ['x']}", "unknown env-file key 'WFT_NOPE'"),
    ("uat: {WFT_SHARE_ROOT: ['(unclosed']}", "isn't a valid regex"),
])
def test_invalid_local_file_stops_start_up_naming_the_file(tmp_path, content, problem):
    (tmp_path / "environments.yaml").write_text(content)
    env_file(tmp_path)
    with pytest.raises(ConfigError) as exc:
        load_config("uat", home=tmp_path)
    assert str(tmp_path / "environments.yaml") in str(exc.value) and problem in str(exc.value)


def test_secrets_never_shown_even_if_a_local_rule_targets_them(tmp_path):
    (tmp_path / "environments.yaml").write_text(yaml.safe_dump({"uat": {"WFT_TT_APP_SECRET": ["^uat-"]}}))
    env_file(tmp_path, **tt("https://tt-uat.example.invalid"))
    with pytest.raises(ConfigError) as exc:
        load_config("uat", home=tmp_path)
    assert "super-secret-value" not in str(exc.value) and "<hidden>" in str(exc.value)


def test_scripts_use_the_local_rules(tmp_path, monkeypatch, capsys):
    import scripts.validate as val
    (tmp_path / "environments.yaml").write_text(yaml.safe_dump(LOCAL))
    env_file(tmp_path, **tt("https://tt-prod.example.invalid"))
    monkeypatch.setenv("WFT_HOME", str(tmp_path))
    assert val.main(["--env", "uat"]) == 2
    assert "does not look like a uat value" in capsys.readouterr().err


def test_check_environment_semantics_unchanged():
    rules = {"uat": {"WFT_SHARE_ROOT": ["uat"]}, "prod": {"WFT_SHARE_ROOT": ["prod"]}}
    check_environment("uat", {"WFT_SHARE_ROOT": "/x/uat"}, rules)
    with pytest.raises(ConfigError):
        check_environment("uat", {"WFT_SHARE_ROOT": "/x/prod"}, rules)
