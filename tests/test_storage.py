"""storage.py's index bookkeeping — the part that has already cost this
project real bugs: observation counts double-counted by a same-day re-run
(2026-08-31), and flagged prices wiped by an unrelated ingest (2026-09-05).
Both regressions are pinned here so they can't come back quietly."""
from datetime import date, timedelta

import pandas as pd

import storage
from conftest import _at, _row

SWEEP = date(2026, 10, 2)
DEPART, RET = date(2026, 10, 10), date(2026, 10, 11)


def _touch(price, when):
    """One freshly fetched row for the seeded fare, at a new price."""
    return pd.DataFrame([_row("LGW", "TOS", DEPART, RET, price, when, "FR", "123")], columns=storage.ROW_COLUMNS)


def _row_for(index_df, dest="TOS"):
    return index_df[index_df["destination"] == dest].iloc[0]


def test_flagged_prices_survive_an_unrelated_ingest(store):
    store.seed(SWEEP, DEPART, RET, [200.0] * 6, 100.0, flagged_weekend=100.0, flagged_holiday=90.0)

    changed, updated = storage.filter_changed(_touch(120.0, _at(SWEEP + timedelta(days=1))), store.index())

    assert len(changed) == 1, "a new price is a change"
    row = _row_for(updated)
    assert row["flagged_min_price_weekend"] == 100.0
    assert row["flagged_min_price_holiday"] == 90.0


def test_a_brand_new_fare_starts_unflagged_and_unobserved(store):
    store.seed(SWEEP, DEPART, RET, [200.0] * 6, 100.0, dest="OLD")
    fresh = pd.DataFrame([_row("LGW", "NEW", DEPART, RET, 150.0, _at(SWEEP), "FR", "9")], columns=storage.ROW_COLUMNS)

    _, updated = storage.filter_changed(fresh, store.index())

    new = _row_for(updated, "NEW")
    assert new["observation_count"] == 1
    assert pd.isna(new["flagged_min_price_weekend"]) and pd.isna(new["flagged_min_price_holiday"])
    assert _row_for(updated, "OLD")["observation_count"] == 7, "untouched fares pass through unchanged"


def test_observation_count_goes_up_once_per_calendar_day_not_once_per_run(store):
    store.seed(SWEEP, DEPART, RET, [200.0] * 6, 100.0)  # last seen 08:00 on SWEEP, count 7

    _, same_day = storage.filter_changed(_touch(101.0, _at(SWEEP, hour=20)), store.index())
    assert _row_for(same_day)["observation_count"] == 7, "a second run the same day is not a second night"

    _, next_day = storage.filter_changed(_touch(101.0, _at(SWEEP + timedelta(days=1))), store.index())
    assert _row_for(next_day)["observation_count"] == 8


def test_an_unchanged_price_is_not_rewritten_but_is_still_counted(store):
    store.seed(SWEEP, DEPART, RET, [200.0] * 6, 100.0)

    changed, updated = storage.filter_changed(_touch(100.0, _at(SWEEP + timedelta(days=1))), store.index())

    assert changed.empty
    assert _row_for(updated)["observation_count"] == 8


def test_the_retired_flagged_min_price_column_is_dropped_on_load(store, tmp_path):
    store.seed(SWEEP, DEPART, RET, [200.0] * 6, 100.0)
    legacy = store.index()
    legacy["flagged_min_price"] = 123.0  # the pre-2026-09-19 single column
    storage.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    legacy.to_parquet(storage.INDEX_PATH, index=False)

    loaded = storage.load_index()

    assert set(loaded.columns) == set(storage.INDEX_COLUMNS)


def test_an_index_from_before_the_per_product_columns_gains_them_as_never_flagged(store):
    store.seed(SWEEP, DEPART, RET, [200.0] * 6, 100.0)
    old = store.index().drop(columns=storage.FLAGGED_PRICE_COLUMNS)
    old.to_parquet(storage.INDEX_PATH, index=False)

    loaded = storage.load_index()

    for col in storage.FLAGGED_PRICE_COLUMNS:
        assert loaded[col].isna().all()


def test_an_empty_index_has_every_column_with_usable_dtypes(store):
    empty = storage.load_index()  # nothing on disk yet
    assert list(empty.columns) == storage.INDEX_COLUMNS
    assert str(empty["observation_count"].dtype) == "int64"
    assert all(str(empty[c].dtype) == "float64" for c in storage.FLAGGED_PRICE_COLUMNS)
