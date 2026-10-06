"""detect()'s funnel counts and its read-only mode — the two things the
visibility work (scripts/replay.py, the heartbeat funnel, the weekly owner
report) stands on. If these counts were wrong, every "why was it quiet?"
answer built on them would be confidently wrong.
"""
from datetime import date

import pandas as pd

import detect

SWEEP = date(2026, 10, 2)  # a Friday


def _seed_one_fare_per_gate(store):
    """Six fares, each built to fall out of the funnel at a known gate."""
    old = [200.0] * 6
    # A: passes everything
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), old, 100.0, dest="AAA")
    # B: not a weekend shape (Mon -> Wed)
    store.seed(SWEEP, date(2026, 10, 12), date(2026, 10, 14), old, 100.0, dest="BBB")
    # C: right shape but 36 days out
    store.seed(SWEEP, date(2026, 11, 7), date(2026, 11, 8), old, 100.0, dest="CCC")
    # D: close and the right shape, but only seen on 2 nights
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), old, 100.0, dest="DDD", obs_count=2)
    # E: seen enough, but not a new low (it was £90 recently)
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), [200.0] * 5 + [90.0], 120.0, dest="EEE")
    # F: a new low, but only 15% below typical
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), old, 170.0, dest="FFF")


def test_funnel_counts_every_gate_in_order(store, detect_cfg):
    _seed_one_fare_per_gate(store)
    stats = {}

    flags = detect.detect(SWEEP, "weekend", config=detect_cfg, stats=stats, persist=False)

    assert [f["destination"] for f in flags] == ["AAA"]
    assert stats == {
        "changed": 6,
        "right_shape": 5,  # all but B
        "within_lead_cap": 4,  # ... minus C
        "seen_enough": 3,  # ... minus D
        "has_history": 3,
        "new_low": 2,  # ... minus E
        "drop_ok": 1,  # ... minus F
        "flagged": 1,
    }
    assert list(stats) == list(detect.FUNNEL_STAGES)


def test_funnel_is_per_product(store, detect_cfg):
    _seed_one_fare_per_gate(store)  # early October: nowhere near a school holiday
    stats = {}

    flags = detect.detect(SWEEP, "holiday", config=detect_cfg, stats=stats, persist=False)

    assert flags == []
    assert stats["changed"] == 6
    assert stats["right_shape"] == 0
    assert all(stats[s] == 0 for s in detect.FUNNEL_STAGES if s != "changed")


def test_funnel_is_zeroed_not_missing_when_there_was_no_sweep_that_night(store, detect_cfg):
    stats = {}
    assert detect.detect(date(2026, 1, 1), "weekend", config=detect_cfg, stats=stats) == []
    assert stats == dict.fromkeys(detect.FUNNEL_STAGES, 0)


def test_persist_false_never_touches_the_index(store, detect_cfg):
    _seed_one_fare_per_gate(store)
    before = store.index().copy()

    flags = detect.detect(SWEEP, "weekend", config=detect_cfg, persist=False)

    assert len(flags) == 1
    pd.testing.assert_frame_equal(store.index(), before)


def test_persist_true_stamps_the_price_so_the_same_fare_does_not_flag_twice(store, detect_cfg):
    _seed_one_fare_per_gate(store)

    first = detect.detect(SWEEP, "weekend", config=detect_cfg)
    again = detect.detect(SWEEP, "weekend", config=detect_cfg)

    assert len(first) == 1 and again == []
    idx = store.index().set_index("destination")
    assert idx.loc["AAA", "flagged_min_price_weekend"] == 100.0
    assert pd.isna(idx.loc["AAA", "flagged_min_price_holiday"]), "a weekend flag must not touch the holiday column"


def test_gate_order_does_not_change_which_fares_flag(store, detect_cfg):
    """The gates were reordered (shape, lead, seen, ...) so the funnel
    reads naturally. They're ANDed, so the result must equal what you get
    by applying each gate independently — checked here against a direct
    re-derivation rather than the code under test."""
    _seed_one_fare_per_gate(store)
    flags = {f["destination"] for f in detect.detect(SWEEP, "weekend", config=detect_cfg, persist=False)}

    expected = set()
    for dest, depart, ret, hist, tonight, obs in [
        ("AAA", date(2026, 10, 10), date(2026, 10, 11), [200.0] * 6, 100.0, 7),
        ("BBB", date(2026, 10, 12), date(2026, 10, 14), [200.0] * 6, 100.0, 7),
        ("CCC", date(2026, 11, 7), date(2026, 11, 8), [200.0] * 6, 100.0, 7),
        ("DDD", date(2026, 10, 10), date(2026, 10, 11), [200.0] * 6, 100.0, 2),
        ("EEE", date(2026, 10, 10), date(2026, 10, 11), [200.0] * 5 + [90.0], 120.0, 7),
        ("FFF", date(2026, 10, 10), date(2026, 10, 11), [200.0] * 6, 170.0, 7),
    ]:
        shape = detect.is_weekend_trip(pd.Timestamp(depart), pd.Timestamp(ret))
        near = (depart - SWEEP).days <= 21
        seen = obs >= 5
        new_low = tonight <= min(hist)
        median = sorted(hist)[len(hist) // 2 - 1 : len(hist) // 2 + 1]
        median = sum(median) / len(median)
        dropped = tonight <= median * 0.8
        if shape and near and seen and new_low and dropped:
            expected.add(dest)
    assert flags == expected == {"AAA"}
