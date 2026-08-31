#!/usr/bin/env python3
"""
Flight Deal Scanner — storage layer (PLAN.md §4).

Three jobs, all against the real /v1/prices/cheap payload confirmed in
step 1 (PLAN.md §2), not a guessed shape:

  1. normalize_response()  — raw API JSON -> rows matching the schema.
  2. write_delta()          — changed-rows-only write to a small dated file.
  3. compact_deltas()        — fold old deltas into per-month Parquet.
     rollup_stale()          — collapse raw rows older than raw_days into
                               weekly min/p05/p25/p50/count summaries.

Deltas exist so git never has to store a full monthly-file rewrite every
night (Parquet is compressed binary; a one-row change produces a globally
different file, and git can't delta that) — see PLAN.md §4 for why.

Layout:
    data/deltas/YYYY-MM-DD.parquet   one small file per sweep night
    data/monthly/YYYY-MM.parquet     compacted, full-resolution history
    data/rollups/YYYY-MM.parquet     weekly summaries of rows older than
                                      retention.raw_days
    data/index/latest.parquet        dedup state — last known price hash
                                      per (origin_airport, destination,
                                      depart_date, return_date, trip_type).
                                      Committed like everything else in
                                      data/, since a fresh checkout has no
                                      other memory of "what changed."

This state index isn't in PLAN.md's row schema (that table describes rows,
not pipeline bookkeeping) — noted here since it's a real file that gets
committed, not an implementation detail hidden inside a run.
"""
import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from config import REPO_ROOT

DATA_DIR = REPO_ROOT / "data"
DELTA_DIR = DATA_DIR / "deltas"
MONTHLY_DIR = DATA_DIR / "monthly"
ROLLUP_DIR = DATA_DIR / "rollups"
INDEX_PATH = DATA_DIR / "index" / "latest.parquet"

KEY_COLUMNS = ["origin_airport", "destination", "depart_date", "return_date", "trip_type"]
ROW_COLUMNS = KEY_COLUMNS + [
    "depart_month",
    "price_gbp",
    "flight_number",
    "airline",
    "expires_at",
    "observed_at",
    "price_hash",
]
INDEX_COLUMNS = KEY_COLUMNS + ["price_hash", "observation_count", "first_seen", "last_seen"]


def _today_utc():
    return datetime.now(timezone.utc).date()


def _parse_iso(ts_str):
    """Parse an ISO8601 timestamp, tolerating a trailing 'Z'.

    datetime.fromisoformat() only accepts 'Z' from Python 3.11 — this repo
    targets 3.12 in CI (PATTERNS.md) but gets exercised locally on whatever
    the developer has (3.9 here), so handle both rather than assume.
    """
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    return datetime.fromisoformat(ts_str)


def _price_hash(price_gbp, airline, flight_number):
    """Hash only the fields that mean 'this is a materially different
    observation'. expires_at and observed_at deliberately excluded — both
    change on every sweep regardless of whether the fare itself did, and
    including them would make every row look 'changed' every night,
    defeating the entire point of changed-rows-only storage.
    """
    raw = f"{price_gbp}|{airline}|{flight_number}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_response(body, origin_airport, observed_at=None):
    """Turn one /v1/prices/cheap response into rows matching the schema.

    `body` is the full parsed JSON (top-level dict with 'data'), exactly
    what sweep.py's fetch() returns after resp.json() — not just the
    inner 'data' dict — so the call site doesn't need to know the shape.

    Raises ValueError on a response that isn't shaped as expected, rather
    than silently skipping it — an API surface that "shifted recently"
    once (per the brief) can shift again, and a quiet skip here would look
    exactly like a route with a genuinely empty result.
    """
    if not isinstance(body, dict) or "data" not in body:
        raise ValueError(
            f"Unexpected /v1/prices/cheap response shape: "
            f"top-level keys were {sorted(body.keys()) if isinstance(body, dict) else type(body).__name__}"
        )
    observed_at = observed_at or datetime.now(timezone.utc)

    rows = []
    for destination, tiers in body["data"].items():
        if not isinstance(tiers, dict):
            raise ValueError(
                f"Unexpected shape under destination {destination!r}: "
                f"expected a dict of tiers, got {type(tiers).__name__}"
            )
        for ticket in tiers.values():
            departure_at = _parse_iso(ticket["departure_at"])
            return_at_raw = ticket.get("return_at")
            return_at = _parse_iso(return_at_raw) if return_at_raw else None
            trip_type = "round_trip" if return_at else "one_way"
            price_gbp = float(ticket["price"])
            airline = ticket.get("airline")
            flight_number = str(ticket.get("flight_number"))
            expires_at_raw = ticket.get("expires_at")

            rows.append(
                {
                    "origin_airport": origin_airport,
                    "destination": destination,
                    "depart_date": pd.Timestamp(departure_at.date()),
                    "return_date": pd.Timestamp(return_at.date()) if return_at else pd.NaT,
                    "trip_type": trip_type,
                    "depart_month": departure_at.strftime("%Y-%m"),
                    "price_gbp": price_gbp,
                    "flight_number": flight_number,
                    "airline": airline,
                    "expires_at": pd.Timestamp(_parse_iso(expires_at_raw)) if expires_at_raw else pd.NaT,
                    "observed_at": pd.Timestamp(observed_at),
                    "price_hash": _price_hash(price_gbp, airline, flight_number),
                }
            )

    return pd.DataFrame(rows, columns=ROW_COLUMNS)


def _empty_timestamp_series(index):
    """An all-NaT column that's explicitly tz-aware UTC from the start.

    Left implicit, pandas gives an empty/NaT datetime column a generic
    dtype that silently loses timezone-awareness on the next parquet
    round-trip — found by testing, not by inspection — and then a later
    comparison against a genuinely tz-aware timestamp raises rather than
    quietly doing the wrong thing. Being explicit here avoids the whole
    class of bug instead of patching one occurrence of it.
    """
    return pd.Series(pd.NaT, index=index, dtype="datetime64[ns, UTC]")


def load_index():
    if not INDEX_PATH.exists():
        empty = pd.DataFrame(columns=INDEX_COLUMNS)
        empty["observation_count"] = empty["observation_count"].astype("int64")
        empty["first_seen"] = _empty_timestamp_series(empty.index)
        empty["last_seen"] = _empty_timestamp_series(empty.index)
        return empty

    index_df = pd.read_parquet(INDEX_PATH)
    # Migrate an index written before observation tracking existed, rather
    # than requiring a one-off backfill script. observation_count=1 for
    # every pre-existing key undercounts their true history (they may have
    # been seen several nights already) — never overcounts — so it just
    # means Phase 2's 5-night eligibility gate (PLAN.md §7's plan, now
    # built) takes a little longer to clear for keys that predate this
    # column than it "truly" should. Safe, and simpler than trying to
    # reconstruct counts that were never recorded.
    if "observation_count" not in index_df.columns:
        index_df["observation_count"] = 1
    if "first_seen" not in index_df.columns:
        index_df["first_seen"] = _empty_timestamp_series(index_df.index)
    if "last_seen" not in index_df.columns:
        index_df["last_seen"] = _empty_timestamp_series(index_df.index)
    return index_df


def save_index(index_df):
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    index_df.to_parquet(INDEX_PATH, index=False, compression="zstd")


def filter_changed(rows_df, index_df):
    """Split freshly-fetched rows into (changed_rows, updated_index).

    A row counts as changed (and gets written to tonight's delta) if its
    key isn't in the index yet, or its price_hash differs from what's
    there. But *every* key fetched tonight — changed or not — gets its
    observation bookkeeping updated: this is what makes "seen on N
    different nights" (Phase 2's eligibility gate) a real counted fact
    rather than a guess from how often the price happened to move.
    Unrelated existing index entries (routes not touched by this call)
    pass through untouched.
    """
    if rows_df.empty:
        return rows_df, index_df

    merged = rows_df.merge(
        index_df[KEY_COLUMNS + ["price_hash", "observation_count", "first_seen", "last_seen"]],
        on=KEY_COLUMNS,
        how="left",
        suffixes=("", "_known"),
    )
    is_new_key = merged["price_hash_known"].isna()
    changed_mask = is_new_key | (merged["price_hash"] != merged["price_hash_known"])
    changed = rows_df[changed_mask.values].reset_index(drop=True)

    # observation_count increments at most once per calendar day, not once
    # per sweep run — found the hard way when a manual verification
    # trigger and the natural nightly run both landed on 2026-08-29,
    # double-counting every LHR key that already existed and letting two
    # routes reach "5 nights" on data that was really 4 (PLAN.md's Phase 2
    # section has the full story). A second run later the same day now
    # sees last_seen already stamped with today's date and skips the
    # increment; a genuinely new calendar day always increments.
    last_seen_date = merged["last_seen"].dt.date
    today_date = rows_df["observed_at"].dt.date
    already_counted_today = (~is_new_key) & (last_seen_date == today_date)
    increment = (~already_counted_today).astype("int64")

    updated_rows = rows_df[KEY_COLUMNS + ["price_hash", "observed_at"]].copy()
    updated_rows["observation_count"] = (
        merged["observation_count"].fillna(0).infer_objects(copy=False).astype("int64")
        + increment.values
    )
    # NOT `.values` here — `.values` on a tz-aware Series silently strips
    # timezone-awareness (a real, found-by-testing pandas gotcha), which
    # then makes this column incomparable to genuinely tz-aware timestamps
    # later. `merged` and `updated_rows` share row order/count from the
    # left join above (index_df's keys are unique by construction), so
    # assigning the Series directly aligns correctly.
    updated_rows["first_seen"] = merged["first_seen"]
    updated_rows.loc[is_new_key.values, "first_seen"] = updated_rows.loc[
        is_new_key.values, "observed_at"
    ]
    updated_rows["last_seen"] = updated_rows["observed_at"]
    updated_rows = updated_rows.drop(columns=["observed_at"])

    untouched = index_df[
        ~index_df.set_index(KEY_COLUMNS).index.isin(rows_df.set_index(KEY_COLUMNS).index)
    ]
    parts = [p for p in (untouched, updated_rows) if not p.empty]
    updated_index = pd.concat(parts, ignore_index=True) if parts else index_df
    return changed, updated_index


def write_delta(changed_df, sweep_date=None):
    """Write only the changed rows for this sweep to a small dated file.

    Returns the path written, or None if nothing changed — a quiet night
    is a valid outcome and must not be treated as an error (PATTERNS.md §7:
    "an empty result is a valid result").
    """
    if changed_df.empty:
        return None
    sweep_date = sweep_date or _today_utc()
    DELTA_DIR.mkdir(parents=True, exist_ok=True)
    path = DELTA_DIR / f"{sweep_date.isoformat()}.parquet"
    if path.exists():
        # Same-day re-run (e.g. a retried Action): append rather than clobber.
        existing = pd.read_parquet(path)
        changed_df = pd.concat([existing, changed_df], ignore_index=True)
    changed_df.to_parquet(path, index=False, compression="zstd")
    return path


def _delta_date(path):
    return datetime.strptime(path.stem, "%Y-%m-%d").date()


def compact_deltas(as_of=None, keep_recent_days=3):
    """Fold delta files older than `keep_recent_days` into per-month
    Parquet files, then delete the folded deltas.

    Deltas are for git efficiency, monthly files are for query efficiency
    (PLAN.md §4) — this is the step that moves data from one shape to the
    other. Recent deltas are left alone so a same-day re-run (write_delta's
    append case) still has something to append to.
    """
    as_of = as_of or _today_utc()
    cutoff = as_of - timedelta(days=keep_recent_days)

    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    to_fold = [
        f for f in sorted(DELTA_DIR.glob("*.parquet")) if _delta_date(f) < cutoff
    ]
    if not to_fold:
        return {"folded_files": 0, "rows_folded": 0, "months_touched": []}

    combined = pd.concat([pd.read_parquet(f) for f in to_fold], ignore_index=True)

    for depart_month, group in combined.groupby("depart_month"):
        monthly_path = MONTHLY_DIR / f"{depart_month}.parquet"
        if monthly_path.exists():
            group = pd.concat([pd.read_parquet(monthly_path), group], ignore_index=True)
        group.to_parquet(monthly_path, index=False, compression="zstd")

    for f in to_fold:
        f.unlink()

    return {
        "folded_files": len(to_fold),
        "rows_folded": len(combined),
        "months_touched": sorted(combined["depart_month"].unique().tolist()),
    }


def rollup_stale(as_of=None, raw_days=120):
    """Collapse raw rows with observed_at older than `raw_days` into
    weekly (route, depart_month, trip_type, lead_time_bucket) summaries —
    min/p05/p25/p50/count, never mean-only (PLAN.md §4: the cheap end of
    the distribution is the whole point, and can't be reconstructed once
    raw rows are gone).

    lead_time_bucket = 30-day buckets of (depart_date - observed_at),
    clipped at 0. Not specified in PLAN.md §4 beyond "keep lead time" —
    this is the concrete choice made here, recorded for visibility since
    it shapes what Phase 2's year-one detector can resolve.
    """
    as_of = as_of or _today_utc()
    cutoff = pd.Timestamp(as_of - timedelta(days=raw_days), tz=None)

    ROLLUP_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"months_processed": 0, "rows_rolled": 0, "rows_kept_raw": 0}

    for monthly_path in sorted(MONTHLY_DIR.glob("*.parquet")):
        df = pd.read_parquet(monthly_path)
        if df.empty:
            continue

        observed_at = pd.to_datetime(df["observed_at"]).dt.tz_localize(None)
        stale_mask = observed_at < cutoff
        if not stale_mask.any():
            continue

        stale = df[stale_mask].copy()
        recent = df[~stale_mask]

        lead_time_days = (
            pd.to_datetime(stale["depart_date"])
            - pd.to_datetime(stale["observed_at"]).dt.tz_localize(None).dt.normalize()
        ).dt.days.clip(lower=0)
        stale["lead_time_bucket"] = (lead_time_days // 30) * 30
        iso = pd.to_datetime(stale["observed_at"]).dt.isocalendar()
        stale["iso_year"] = iso["year"].values
        stale["iso_week"] = iso["week"].values

        group_cols = [
            "origin_airport",
            "destination",
            "depart_month",
            "trip_type",
            "lead_time_bucket",
            "iso_year",
            "iso_week",
        ]
        agg = (
            stale.groupby(group_cols)["price_gbp"]
            .agg(
                min_price="min",
                p05_price=lambda s: s.quantile(0.05),
                p25_price=lambda s: s.quantile(0.25),
                p50_price=lambda s: s.quantile(0.50),
                count="count",
            )
            .reset_index()
        )

        rollup_path = ROLLUP_DIR / monthly_path.name
        if rollup_path.exists():
            # group_cols include (iso_year, iso_week), a calendar week that
            # only ever occurs once and is never revisited by a forward-
            # moving sweep, so existing and new rollup rows shouldn't share
            # a key. Concatenating rather than merging avoids silently
            # averaging percentiles together if that assumption is ever
            # wrong — a duplicate key should be visible, not smoothed over.
            agg = pd.concat([pd.read_parquet(rollup_path), agg], ignore_index=True)
        agg.to_parquet(rollup_path, index=False, compression="zstd")

        summary["rows_rolled"] += len(stale)
        summary["rows_kept_raw"] += len(recent)
        summary["months_processed"] += 1

        if recent.empty:
            monthly_path.unlink()
        else:
            recent.to_parquet(monthly_path, index=False, compression="zstd")

    return summary


def load_full_history(depart_months=None):
    """Every recorded price row for the given depart_months (default: all),
    combining not-yet-compacted deltas with compacted monthly files.

    Nothing else in storage.py needed to read both sources together before
    — sweeps only ever write, compaction only ever moves data from one
    shape to the other. detect.py (Phase 2) is the first thing that needs
    a flight's *complete* history regardless of which shape it's currently
    sitting in, so this is the one place that combines them.
    """
    frames = []
    if MONTHLY_DIR.exists():
        for f in sorted(MONTHLY_DIR.glob("*.parquet")):
            if depart_months is None or f.stem in depart_months:
                frames.append(pd.read_parquet(f))
    if DELTA_DIR.exists():
        for f in sorted(DELTA_DIR.glob("*.parquet")):
            df = pd.read_parquet(f)
            if depart_months is not None:
                df = df[df["depart_month"].isin(depart_months)]
            if not df.empty:
                frames.append(df)
    if not frames:
        return pd.DataFrame(columns=ROW_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def stats():
    """Quick inspection of current data/ state — row/file counts, not content."""
    def _count(pattern, base_dir):
        files = sorted(base_dir.glob(pattern)) if base_dir.exists() else []
        rows = sum(len(pd.read_parquet(f)) for f in files)
        return len(files), rows

    delta_files, delta_rows = _count("*.parquet", DELTA_DIR)
    monthly_files, monthly_rows = _count("*.parquet", MONTHLY_DIR)
    rollup_files, rollup_rows = _count("*.parquet", ROLLUP_DIR)
    index_rows = len(load_index())

    return {
        "deltas": {"files": delta_files, "rows": delta_rows},
        "monthly": {"files": monthly_files, "rows": monthly_rows},
        "rollups": {"files": rollup_files, "rows": rollup_rows},
        "index_rows": index_rows,
    }


def ingest(body, origin_airport, observed_at=None, sweep_date=None):
    """One call: normalize a raw response, diff against the index, write
    the delta, and persist the updated index. This is what sweep.py (step
    3) calls per origin per month.
    """
    rows = normalize_response(body, origin_airport, observed_at)
    index_df = load_index()
    changed, updated_index = filter_changed(rows, index_df)
    path = write_delta(changed, sweep_date)
    save_index(updated_index)

    cheapest = None
    if not rows.empty:
        best = rows.loc[rows["price_gbp"].idxmin()]
        cheapest = {
            "origin_airport": best["origin_airport"],
            "destination": best["destination"],
            "price_gbp": float(best["price_gbp"]),
        }

    return {
        "fetched": len(rows),
        "changed": len(changed),
        "delta_path": path,
        "cheapest": cheapest,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true", help="Fold old deltas into monthly files.")
    parser.add_argument("--rollup", action="store_true", help="Collapse stale raw rows into weekly summaries.")
    parser.add_argument("--stats", action="store_true", help="Print current data/ row and file counts.")
    parser.add_argument("--keep-recent-days", type=int, default=3, help="Deltas newer than this are left uncompacted.")
    parser.add_argument("--raw-days", type=int, default=120, help="Raw rows older than this get rolled up.")
    args = parser.parse_args()

    if not (args.compact or args.rollup or args.stats):
        parser.error("Specify at least one of --compact, --rollup, --stats.")

    if args.compact:
        print("compact_deltas:", compact_deltas(keep_recent_days=args.keep_recent_days))
    if args.rollup:
        print("rollup_stale:", rollup_stale(raw_days=args.raw_days))
    if args.stats:
        print("stats:", stats())


if __name__ == "__main__":
    main()
