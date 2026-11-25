"""The real config must load, and a broken config must fail with a useful message."""

from __future__ import annotations

import base64
import json

import pytest
import yaml

from tula_insights.config import ConfigError, Env, load_config


def write(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_repository_config_is_valid(config_dir):
    config = load_config()
    assert len(config.categories.categories) == 10
    assert "general_pet_care" in config.categories.ids
    assert [s.name for s in config.enabled_sources] == ["reddit_search"]


def test_source_defaults_are_filled_in(config_dir):
    config = load_config()
    reddit = next(s for s in config.sources.resolve() if s.name == "reddit_search")
    # Set explicitly on the source.
    assert reddit.daily_request_cap == 20
    # Inherited from the defaults block.
    assert reddit.timeout_seconds == 30.0
    assert reddit.max_retries == 4


def test_disabled_sources_are_excluded(config_dir):
    config = load_config()
    names = {s.name for s in config.enabled_sources}
    assert "youtube" not in names


def test_weights_must_sum_to_one(config_dir):
    path = config_dir / "scoring.yaml"
    data = yaml.safe_load(path.read_text())
    data["weights"]["recurrence"] = 0.5
    write(path, data)
    with pytest.raises(ConfigError, match=r"sum to 1\.0"):
        load_config()


def test_judge_band_must_not_be_empty(config_dir):
    path = config_dir / "scoring.yaml"
    data = yaml.safe_load(path.read_text())
    data["dedupe"]["judge_lower_threshold"] = 0.95
    write(path, data)
    with pytest.raises(ConfigError, match="judge_lower_threshold"):
        load_config()


def test_keyword_set_must_name_a_real_category(config_dir):
    path = config_dir / "keywords.yaml"
    data = yaml.safe_load(path.read_text())
    data["keyword_sets"][0]["category"] = "not_a_category"
    write(path, data)
    with pytest.raises(ConfigError, match=r"not in categories\.yaml"):
        load_config()


def test_keyword_set_must_name_a_real_region(config_dir):
    path = config_dir / "keywords.yaml"
    data = yaml.safe_load(path.read_text())
    data["keyword_sets"][0]["regions"] = ["atlantis"]
    write(path, data)
    with pytest.raises(ConfigError, match="atlantis"):
        load_config()


def test_every_category_needs_a_keyword_set(config_dir):
    path = config_dir / "keywords.yaml"
    data = yaml.safe_load(path.read_text())
    data["keyword_sets"] = [k for k in data["keyword_sets"] if k["category"] != "costs_planning"]
    write(path, data)
    with pytest.raises(ConfigError, match="costs_planning"):
        load_config()


def test_angle_model_must_have_a_price(config_dir):
    path = config_dir / "scoring.yaml"
    data = yaml.safe_load(path.read_text())
    data["publish"]["angle_model"] = "claude-not-a-model"
    write(path, data)
    with pytest.raises(ConfigError, match="no price"):
        load_config()


def test_publish_rules_must_be_satisfiable(config_dir):
    path = config_dir / "scoring.yaml"
    data = yaml.safe_load(path.read_text())
    data["publish"]["max_per_category"] = 99
    write(path, data)
    with pytest.raises(ConfigError, match="max_per_category"):
        load_config()


def test_duplicate_category_ids_are_rejected(config_dir):
    path = config_dir / "categories.yaml"
    data = yaml.safe_load(path.read_text())
    data["categories"].append(dict(data["categories"][0]))
    write(path, data)
    with pytest.raises(ConfigError, match="duplicate category id"):
        load_config()


def test_unknown_key_is_a_typo_not_an_extension(config_dir):
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["sheet"]["cell_limitt"] = 5
    write(path, data)
    with pytest.raises(ConfigError, match="cell_limitt"):
        load_config()


def test_missing_file_names_the_file(config_dir):
    (config_dir / "budget.yaml").unlink()
    with pytest.raises(ConfigError, match=r"budget\.yaml"):
        load_config()


def test_input_token_cap_must_stay_under_long_context(config_dir):
    path = config_dir / "budget.yaml"
    data = yaml.safe_load(path.read_text())
    data["limits"]["max_input_tokens_per_request"] = 150000
    write(path, data)
    with pytest.raises(ConfigError, match="long_context_threshold_tokens"):
        load_config()


# --- environment ---------------------------------------------------------------------

SAMPLE_KEY = {
    "type": "service_account",
    "client_email": "bot@example.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",  # pragma: allowlist secret
    "token_uri": "https://oauth2.googleapis.com/token",
}


def test_service_account_accepts_base64():
    raw = json.dumps(SAMPLE_KEY)
    encoded = base64.b64encode(raw.encode()).decode()
    env = Env(google_service_account_json=encoded)
    assert env.service_account_info()["client_email"] == SAMPLE_KEY["client_email"]


def test_service_account_accepts_raw_json():
    env = Env(google_service_account_json=json.dumps(SAMPLE_KEY))
    assert env.service_account_info()["client_email"] == SAMPLE_KEY["client_email"]


def test_service_account_rejects_rubbish():
    env = Env(google_service_account_json="this is not a key")
    with pytest.raises(ConfigError, match="neither valid base64 nor raw JSON"):
        env.service_account_info()


def test_service_account_rejects_incomplete_key():
    incomplete = {"client_email": "a@b.com"}
    env = Env(google_service_account_json=json.dumps(incomplete))
    with pytest.raises(ConfigError, match="private_key"):
        env.service_account_info()


def test_require_names_every_missing_variable():
    env = Env()
    with pytest.raises(ConfigError) as caught:
        env.require("sheet_id", "anthropic_api_key")
    assert "SHEET_ID" in str(caught.value)
    assert "ANTHROPIC_API_KEY" in str(caught.value)
