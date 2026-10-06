"""scripts/replay.py — the "preview a threshold change before it reaches
anyone" tool. Its two promises are that it can't write anything and that a
mistyped setting name can't silently replay the *current* behaviour."""
from datetime import date

import pandas as pd
import pytest

import replay
from conftest import DETECT_CFG

SWEEP = date(2026, 10, 2)


def test_overrides_are_typed_and_leave_the_original_config_alone():
    base = {"detection": {"drop_pct_threshold": 0.2, "max_lead_days": 21, "enabled": False}}

    out = replay.apply_overrides(
        base, ["detection.drop_pct_threshold=0.35", "detection.max_lead_days=28", "detection.enabled=true"]
    )

    assert out["detection"] == {"drop_pct_threshold": 0.35, "max_lead_days": 28, "enabled": True}
    assert base["detection"]["drop_pct_threshold"] == 0.2, "the caller's config must not be mutated"


@pytest.mark.parametrize(
    "bad",
    [
        "detection.drop_pct_treshold=0.3",  # typo'd leaf
        "detecton.drop_pct_threshold=0.3",  # typo'd section
        "detection=0.3",  # replacing a whole section
        "detection.drop_pct_threshold",  # no value
        "=0.3",
    ],
)
def test_unknown_or_malformed_settings_are_rejected_not_ignored(bad):
    with pytest.raises(SystemExit):
        replay.apply_overrides({"detection": {"drop_pct_threshold": 0.2}}, [bad])


def _seed(store):
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), [200.0] * 6, 150.0, dest="AAA")  # 25% below


def test_an_override_changes_the_outcome_and_the_real_config_does_not(store):
    _seed(store)
    base = DETECT_CFG

    at_20 = replay.replay(base, [SWEEP])
    at_30 = replay.replay(replay.apply_overrides(base, ["detection.drop_pct_threshold=0.30"]), [SWEEP])

    assert replay._totals(at_20) == {"holiday": 0, "weekend": 1}
    assert replay._totals(at_30) == {"holiday": 0, "weekend": 0}


def test_replay_is_read_only(store):
    _seed(store)
    before = store.index().copy()

    replay.replay(DETECT_CFG, [SWEEP])
    replay.replay(DETECT_CFG, [SWEEP])  # twice: a write would make the second differ

    pd.testing.assert_frame_equal(store.index(), before)
    assert not (store.index()["flagged_min_price_weekend"].notna()).any()


def test_available_nights_lists_the_deltas_on_disk_oldest_first(store):
    store.seed(date(2026, 10, 3), date(2026, 10, 10), date(2026, 10, 11), [200.0] * 6, 150.0, dest="AAA")
    store.seed(date(2026, 10, 1), date(2026, 10, 10), date(2026, 10, 11), [200.0] * 6, 150.0, dest="BBB")
    assert replay.available_nights() == [date(2026, 10, 1), date(2026, 10, 3)]
