"""The rules that decide what real people get emailed. Each case here is
one the project has already gotten wrong, nearly got wrong, or had to be
told about — see PLAN.md for the story behind each. Dates are real
2026 calendar days (Fri 2 Oct, Sat 10 Oct, ...); October half-term is
24 Oct - 1 Nov."""
from datetime import date

import pandas as pd
import pytest

import detect

SWEEP = date(2026, 10, 2)  # a Friday


def ts(d):
    return pd.Timestamp(d)


# --- weekend shapes ------------------------------------------------------------


@pytest.mark.parametrize(
    "depart,ret",
    [
        (date(2026, 10, 2), date(2026, 10, 4)),  # Fri -> Sun
        (date(2026, 10, 2), date(2026, 10, 5)),  # Fri -> Mon (added 2026-10-04)
        (date(2026, 10, 3), date(2026, 10, 5)),  # Sat -> Mon
        (date(2026, 10, 3), date(2026, 10, 4)),  # Sat -> Sun
    ],
)
def test_the_four_real_weekend_shapes_qualify(depart, ret):
    assert detect.is_weekend_trip(depart, ret)
    assert detect.is_weekend_trip(ts(depart), ts(ret)), "pipeline passes pandas Timestamps, not dates"


@pytest.mark.parametrize(
    "depart,ret",
    [
        (date(2026, 10, 5), date(2026, 10, 7)),  # Mon -> Wed: passed the old generic floor, must not now
        (date(2026, 10, 2), date(2026, 10, 3)),  # Fri -> Sat, one night
        (date(2026, 10, 4), date(2026, 10, 6)),  # Sun -> Tue
        (date(2026, 10, 1), date(2026, 10, 4)),  # Thu -> Sun
        (date(2026, 10, 3), date(2026, 10, 3)),  # same day
        (date(2026, 10, 2), date(2026, 10, 11)),  # Fri -> Sun but 9 nights: same weekday pair, wrong week
        (date(2026, 10, 2), date(2026, 10, 12)),  # Fri -> Mon but 10 nights
        (date(2026, 10, 3), date(2026, 10, 12)),  # Sat -> Mon but 9 nights
    ],
)
def test_other_shapes_never_qualify_including_the_wrong_week_trap(depart, ret):
    assert not detect.is_weekend_trip(depart, ret)


# --- holiday window ------------------------------------------------------------


@pytest.mark.parametrize(
    "depart,ret,expected",
    [
        (date(2026, 10, 26), date(2026, 10, 29), True),  # inside
        (date(2026, 10, 20), date(2026, 10, 25), True),  # overlaps the start
        (date(2026, 10, 30), date(2026, 11, 5), True),  # overlaps the end
        (date(2026, 10, 20), date(2026, 11, 5), True),  # spans the whole window
        (date(2026, 10, 20), date(2026, 10, 22), True),  # returns 2 days before: the +/-2 tolerance (2026-10-06 request)
        (date(2026, 11, 3), date(2026, 11, 5), True),  # departs 2 days after
        (date(2026, 10, 19), date(2026, 10, 21), False),  # 3 days out: tolerance unchanged
        (date(2026, 11, 4), date(2026, 11, 6), False),
        (date(2026, 9, 10), date(2026, 9, 15), False),  # nowhere near
        (date(2026, 12, 30), date(2027, 1, 2), True),  # Christmas, across the year boundary
        (date(2027, 1, 5), date(2027, 1, 7), True),  # 2 days after Christmas ends (3 Jan)
        (date(2027, 1, 6), date(2027, 1, 8), False),
    ],
)
def test_holiday_window_with_the_two_day_tolerance(depart, ret, expected):
    assert detect.is_holiday_trip(depart, ret) is expected
    assert detect.is_holiday_trip(ts(depart), ts(ret)) is expected


# --- the detection decision, through detect() ----------------------------------

OLD = [200.0] * 6


def _flagged(store, cfg, product="weekend", sweep=SWEEP):
    return detect.detect(sweep, product, config=cfg, persist=False)


def test_a_qualifying_fare_is_flagged_with_the_full_record(store, detect_cfg):
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), OLD, 100.0, dest="AAA")

    (flag,) = _flagged(store, detect_cfg)

    assert flag == {
        "product": "weekend",
        "flagged_at": "2026-10-02",
        "origin_airport": "LGW",
        "destination": "AAA",
        "depart_date": "2026-10-10",
        "return_date": "2026-10-11",
        "trip_type": "round_trip",
        "price_gbp": 100.0,
        "prior_min_gbp": 200.0,
        "prior_median_gbp": 200.0,
        "drop_pct_vs_median": 0.5,
        "observation_count": 7,
        "airline": "FR",
        "flight_number": "123",
    }


def test_lead_cap_is_inclusive_at_21_days_and_excludes_22(store, detect_cfg):
    # Both weekend-shaped: Fri 23 Oct is 21 days from Fri 2 Oct, Sat 24 Oct is 22.
    store.seed(SWEEP, date(2026, 10, 23), date(2026, 10, 25), OLD, 100.0, dest="D21")
    store.seed(SWEEP, date(2026, 10, 24), date(2026, 10, 25), OLD, 100.0, dest="D22")

    assert [f["destination"] for f in _flagged(store, detect_cfg)] == ["D21"]


def test_a_fare_needs_enough_nights_of_history(store, detect_cfg):
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), OLD, 100.0, dest="FOUR", obs_count=4)
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), OLD, 100.0, dest="FIVE", obs_count=5)

    assert [f["destination"] for f in _flagged(store, detect_cfg)] == ["FIVE"]


def test_drop_threshold_boundary(store, detect_cfg):
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), OLD, 159.0, dest="ENOUGH")  # 20.5% below
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), OLD, 161.0, dest="SHORT")  # 19.5% below

    assert [f["destination"] for f in _flagged(store, detect_cfg)] == ["ENOUGH"]


def test_a_fare_that_is_not_a_new_low_is_not_flagged_however_far_below_median(store, detect_cfg):
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), [200.0] * 5 + [90.0], 120.0, dest="AAA")
    assert _flagged(store, detect_cfg) == []


def test_new_low_only_looks_back_the_configured_window(store, detect_cfg):
    """A £50 fare 300 days ago must not block flagging forever (the
    2026-10-04 request) — but with a long enough window it does."""
    history = [50.0] + [200.0] * 5
    ages = [300, 5, 4, 3, 2, 1]
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), history, 100.0, dest="AAA", history_days_ago=ages)

    assert len(_flagged(store, detect_cfg)) == 1

    detect_cfg["detection"]["new_low_lookback_days"] = 400
    assert _flagged(store, detect_cfg) == []


def test_a_fare_must_beat_its_own_last_flagged_price_not_tie_it(store, detect_cfg):
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), OLD, 100.0, dest="TIE", flagged_weekend=100.0)
    store.seed(SWEEP, date(2026, 10, 10), date(2026, 10, 11), OLD, 90.0, dest="BETTER", flagged_weekend=100.0)

    assert [f["destination"] for f in _flagged(store, detect_cfg)] == ["BETTER"]


def test_one_fare_can_qualify_for_both_products_and_neither_blocks_the_other(store, detect_cfg):
    # Sat 24 - Sun 25 Oct: a weekend shape *and* inside October half-term, 21 days out.
    sweep = date(2026, 10, 3)
    store.seed(sweep, date(2026, 10, 24), date(2026, 10, 25), OLD, 100.0, dest="BOTH")

    assert [f["destination"] for f in _flagged(store, detect_cfg, "weekend", sweep)] == ["BOTH"]
    assert [f["destination"] for f in _flagged(store, detect_cfg, "holiday", sweep)] == ["BOTH"]


def test_flagging_for_one_product_leaves_the_other_free_to_flag(store, detect_cfg):
    sweep = date(2026, 10, 3)
    store.seed(sweep, date(2026, 10, 24), date(2026, 10, 25), OLD, 100.0, dest="BOTH", flagged_weekend=90.0)

    assert _flagged(store, detect_cfg, "weekend", sweep) == [], "already flagged for weekend at £90"
    assert len(_flagged(store, detect_cfg, "holiday", sweep)) == 1, "the holiday product never flagged it"


def test_a_holiday_trip_that_is_not_a_weekend_shape_flags_only_as_holiday(store, detect_cfg):
    sweep = date(2026, 10, 8)  # Thu; Tue 27 - Sat 31 Oct is during half-term, 19 days out, not a weekend shape
    store.seed(sweep, date(2026, 10, 27), date(2026, 10, 31), [300.0] * 6, 150.0, dest="HOL")

    assert _flagged(store, detect_cfg, "weekend", sweep) == []
    assert [f["destination"] for f in _flagged(store, detect_cfg, "holiday", sweep)] == ["HOL"]


def test_an_unknown_product_is_an_error_not_an_empty_result(store, detect_cfg):
    with pytest.raises(ValueError, match="unknown product"):
        detect.detect(SWEEP, "weekday", config=detect_cfg)


# --- the lookback can't exceed what storage keeps -------------------------------


def test_lookback_is_capped_at_raw_retention_and_says_so_once(capsys):
    cfg = {"detection": {"new_low_lookback_days": 183}, "retention": {"raw_days": 120}}
    detect._WARNED.clear()

    assert detect.effective_new_low_lookback_days(cfg) == 120
    assert detect.effective_new_low_lookback_days(cfg) == 120

    out = capsys.readouterr().out
    assert out.count("effective new-low window is 120 days") == 1


def test_lookback_inside_retention_is_left_alone(capsys):
    cfg = {"detection": {"new_low_lookback_days": 90}, "retention": {"raw_days": 120}}
    assert detect.effective_new_low_lookback_days(cfg) == 90
    assert detect.effective_new_low_lookback_days({"detection": {"new_low_lookback_days": 183}}) == 183
    assert capsys.readouterr().out == ""
