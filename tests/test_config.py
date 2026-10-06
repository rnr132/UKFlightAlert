"""The real config/sweep.yaml and the real workflow, checked structurally.

A mistyped key in the YAML would otherwise only surface as a KeyError in
the scheduled job (or worse, in the one branch that runs on digest nights),
and the workflow's step order is what makes "tests run before anything is
emailed" true — an innocent reshuffle would silently remove it.
"""
from pathlib import Path

import pytest
import yaml

import detect
from config import load_config

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "sweep.yml"

REQUIRED = {
    "api": ["base_url", "token_env_var", "token_header", "endpoint"],
    "market": ["currency"],
    "horizon": ["near_months", "far_months", "far_sweep_weekday"],
    "rate_limit": ["requests_per_minute", "max_retries"],
    "retention": ["raw_days"],
    "detection": ["min_observations", "drop_pct_threshold", "max_lead_days", "new_low_lookback_days"],
    "monitoring": ["staleness_warning_hours"],
    "booking_links": ["trs", "marker", "create_link_path", "sub_id", "shorten", "requests_per_minute"],
    "notify": [
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "from_address",
        "from_name",
        "password_env_var",
        "recipients_env_var",
        "reply_to_env_var",
        "owner_report_weekday",
    ],
}


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.mark.parametrize("section,keys", REQUIRED.items())
def test_every_setting_the_code_reads_exists(cfg, section, keys):
    assert section in cfg, f"config/sweep.yaml is missing the {section!r} section"
    for key in keys:
        assert key in cfg[section], f"config/sweep.yaml: {section}.{key} is missing"


def test_detection_values_are_sane(cfg):
    d = cfg["detection"]
    assert 0 < d["drop_pct_threshold"] < 1
    assert d["max_lead_days"] > 0
    assert d["min_observations"] >= 1
    assert d["new_low_lookback_days"] > 0


def test_the_real_new_low_window_never_claims_more_history_than_storage_keeps(cfg):
    assert detect.effective_new_low_lookback_days(cfg) <= cfg["retention"]["raw_days"]


def test_digest_weekday_style_settings_are_valid_weekdays(cfg):
    assert 0 <= cfg["notify"]["owner_report_weekday"] <= 6
    assert 0 <= cfg["horizon"]["far_sweep_weekday"] <= 6


def test_no_secret_value_is_in_the_committed_config(cfg):
    """Secrets are named by env var, never held. A pasted API key or a real
    address would be caught here before it reached a public repo."""
    text = yaml.safe_dump(cfg)
    assert "re_" not in cfg["notify"]["smtp_username"]
    for name in ("password_env_var", "recipients_env_var", "reply_to_env_var"):
        assert cfg["notify"][name].isupper(), f"notify.{name} should name an env var, not hold a value"
    assert text.count("@") == 1, "the only address in config should be the public sender"


# --- the workflow ----------------------------------------------------------------


@pytest.fixture(scope="module")
def steps():
    wf = yaml.safe_load(WORKFLOW.read_text())
    return wf["jobs"]["sweep"]["steps"]


def _idx(steps, name):
    for i, s in enumerate(steps):
        if s.get("name") == name:
            return i
    raise AssertionError(f"workflow has no step named {name!r}: {[s.get('name') for s in steps]}")


def test_tests_run_after_the_sweep_and_before_anything_is_emailed(steps):
    sweep, tests, digest, commit = (
        _idx(steps, n) for n in ("Run sweep", "Run tests", "Send nightly digest", "Commit data")
    )
    assert sweep < tests < digest < commit


def test_the_test_step_is_allowed_to_fail_the_run(steps):
    step = steps[_idx(steps, "Run tests")]
    assert not step.get("continue-on-error"), "a failing test must stop the digest, not be shrugged off"
    assert "pytest" in step["run"]


def test_data_is_committed_even_when_the_tests_or_digest_fail(steps):
    assert "!cancelled()" in steps[_idx(steps, "Commit data")]["if"]


def test_each_step_gets_only_the_secrets_it_needs(steps):
    sweep_env = steps[_idx(steps, "Run sweep")]["env"]
    digest_env = steps[_idx(steps, "Send nightly digest")]["env"]
    assert set(sweep_env) == {"TRAVELPAYOUTS_TOKEN"}
    assert set(digest_env) == {"SMTP_PASSWORD", "NOTIFY_RECIPIENTS", "REPLY_TO"}
    assert "env" not in steps[_idx(steps, "Run tests")], "tests must never see a real credential"
