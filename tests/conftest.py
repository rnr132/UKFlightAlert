"""Shared fixtures.

`store` is a tiny, hermetic stand-in for data/: monthly history, tonight's
delta, and the index, all in a tmp dir with storage.py / detect.py pointed
at it. Seeding goes through the same parquet schema the real pipeline
writes (storage.ROW_COLUMNS / INDEX_COLUMNS), so a schema change breaks
these tests loudly instead of letting them pass against a shape production
no longer has.
"""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

import detect
import storage

UTC = timezone.utc

# What config/sweep.yaml's detection block looks like, pinned here so the
# rules are tested against known numbers rather than whatever the tunable
# values happen to be this week. (tests/test_config.py separately checks
# the *real* file is internally consistent.)
DETECT_CFG = {
    "detection": {
        "min_observations": 5,
        "drop_pct_threshold": 0.20,
        "max_lead_days": 21,
        "new_low_lookback_days": 183,
    }
}


def _at(day, hour=8):
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _row(origin, dest, depart, ret, price, observed_at, airline, flight):
    return {
        "origin_airport": origin,
        "destination": dest,
        "depart_date": pd.Timestamp(depart),
        "return_date": pd.Timestamp(ret),
        "trip_type": "round_trip",
        "depart_month": depart.strftime("%Y-%m"),
        "price_gbp": float(price),
        "flight_number": str(flight),
        "airline": airline,
        "expires_at": pd.Timestamp(observed_at + timedelta(hours=1)),
        "observed_at": pd.Timestamp(observed_at),
        "price_hash": storage._price_hash(price, airline, flight),
    }


class Store:
    def __init__(self):
        self._history = []
        self._tonight = {}
        self._index = []

    def seed(
        self,
        sweep_date,
        depart,
        ret,
        history,
        tonight,
        *,
        origin="LGW",
        dest="TOS",
        obs_count=None,
        history_days_ago=None,
        flagged_weekend=None,
        flagged_holiday=None,
        airline="FR",
        flight="123",
    ):
        """One fare: `history` prices (oldest first, one per night ending
        the night before `sweep_date`, or at explicit `history_days_ago`),
        then `tonight`'s price as the changed row for `sweep_date`.
        obs_count defaults to however many nights that adds up to."""
        n = len(history)
        ages = history_days_ago if history_days_ago is not None else [n - i for i in range(n)]
        for price, age in zip(history, ages):
            self._history.append(_row(origin, dest, depart, ret, price, _at(sweep_date - timedelta(days=age)), airline, flight))
        self._tonight.setdefault(sweep_date, []).append(
            _row(origin, dest, depart, ret, tonight, _at(sweep_date), airline, flight)
        )
        first = _at(sweep_date - timedelta(days=max(ages))) if ages else _at(sweep_date)
        self._index.append(
            {
                "origin_airport": origin,
                "destination": dest,
                "depart_date": pd.Timestamp(depart),
                "return_date": pd.Timestamp(ret),
                "trip_type": "round_trip",
                "price_hash": storage._price_hash(tonight, airline, flight),
                "observation_count": obs_count if obs_count is not None else n + 1,
                "first_seen": pd.Timestamp(first),
                "last_seen": pd.Timestamp(_at(sweep_date)),
                "flagged_min_price_weekend": float("nan") if flagged_weekend is None else float(flagged_weekend),
                "flagged_min_price_holiday": float("nan") if flagged_holiday is None else float(flagged_holiday),
            }
        )
        self._flush()

    def _flush(self):
        if self._history:
            df = pd.DataFrame(self._history, columns=storage.ROW_COLUMNS)
            for month, group in df.groupby("depart_month"):
                path = storage.MONTHLY_DIR / f"{month}.parquet"
                path.parent.mkdir(parents=True, exist_ok=True)
                group.to_parquet(path, index=False, compression="zstd")
        for day, rows in self._tonight.items():
            storage.DELTA_DIR.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows, columns=storage.ROW_COLUMNS).to_parquet(
                storage.DELTA_DIR / f"{day.isoformat()}.parquet", index=False, compression="zstd"
            )
        idx = pd.DataFrame(self._index, columns=storage.INDEX_COLUMNS)
        idx["observation_count"] = idx["observation_count"].astype("int64")
        for col in storage.FLAGGED_PRICE_COLUMNS:
            idx[col] = idx[col].astype("float64")
        storage.save_index(idx)

    def index(self):
        return storage.load_index()


@pytest.fixture
def store(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(storage, "DATA_DIR", data)
    monkeypatch.setattr(storage, "DELTA_DIR", data / "deltas")
    monkeypatch.setattr(storage, "MONTHLY_DIR", data / "monthly")
    monkeypatch.setattr(storage, "ROLLUP_DIR", data / "rollups")
    monkeypatch.setattr(storage, "INDEX_PATH", data / "index" / "latest.parquet")
    monkeypatch.setattr(detect, "FLAGS_DIR", data / "flags")
    return Store()


@pytest.fixture
def detect_cfg():
    import copy

    return copy.deepcopy(DETECT_CFG)
