#!/usr/bin/env python3
"""
Flight Deal Scanner — deal detection (Phase 2).

Pure arithmetic over data already collected by the sweep — no API calls,
no LLM calls, matching the brief's explicit constraint from Phase 1.

Two hyper-specialized products, not one general-purpose detector
(2026-09-19 pivot — replaces the old "2+ nights, any shape" rule
entirely, not additively). The reasoning: almost nobody can actually act
on a random mid-week, random-length fare found on short notice, so the
system should only ever flag the two shapes a normal person really can —
a spontaneous weekend, or a trip during a school holiday that's booked
months ahead anyway. See PRODUCTS below for the two eligibility rules.

For every flight whose price changed *tonight*, per product: checks
whether tonight's price is both a genuine new low for that flight and
meaningfully below its own recent typical price — AND strictly better
than the last price this exact flight was already flagged at *for that
product*, so a fare that merely holds at its own record low (e.g. a
different flight number now selling the same price) doesn't re-flag
every night it happens to be re-observed. The two products track this
independently (storage.py's flagged_min_price_<product> columns) since
the same fare can legitimately qualify for both at once — a Saturday
flight during half-term is both a weekend trip and a holiday trip, and
flagging it for one shouldn't block the other from ever flagging it.

Flags are written to data/flags/<product>/YYYY-MM-DD.jsonl. Nothing gets
sent anywhere — this step only finds and records candidates; delivery is
notify.py.

Why only tonight's changed rows get evaluated, not the full history every
night: a flight whose price hasn't moved since it was last checked can't
possibly be a *new* low today that wasn't already true then, so
re-evaluating every unchanged row every night would be wasted work —
against the brief's "the eventual percentile-scoring step is a cheap
query" design goal.

Usage:
    python scripts/detect.py                       # both products, tonight (UTC)
    python scripts/detect.py --product weekend      # one product only
    python scripts/detect.py --date 2026-09-15      # a specific past date
"""
import argparse
import json
from datetime import date

import pandas as pd

import school_holidays
import storage
from config import load_config

FLAGS_DIR = storage.DATA_DIR / "flags"


def _key_tuple(row):
    return tuple(row[c] for c in storage.KEY_COLUMNS)


def _as_date_str(value):
    return str(value.date()) if hasattr(value, "date") else str(value)


_WEEKEND_SHAPES = {
    (4, 6): 2,  # Friday -> Sunday
    (5, 0): 2,  # Saturday -> Monday
    (5, 6): 1,  # Saturday -> Sunday
}


def is_weekend_trip(depart_date, return_date):
    """True only for a Friday->Sunday, Saturday->Monday, or
    Saturday->Sunday trip — the three shapes someone can plausibly book on
    short notice without pre-arranged leave. Replaces the old generic
    "2+ nights, any shape" floor and its Sat-Sun exception entirely
    (2026-09-19) — a 2-night midweek trip that used to qualify no longer
    does; this product isn't a superset of the old rule.

    Checks the actual night count against the day-of-week pair, not just
    the pair alone — a Friday departure and a Sunday return three weeks
    later share the same weekday pair as a genuine weekend trip but isn't
    one, so the shape has to match a specific number of nights too.

    Mon=0 ... Sun=6. Takes real date-like objects (anything with
    .weekday() — a datetime.date, a datetime.datetime, or a pandas
    Timestamp all work); callers convert from their own representation."""
    nights = (return_date - depart_date).days
    shape = (depart_date.weekday(), return_date.weekday())
    return _WEEKEND_SHAPES.get(shape) == nights


def is_holiday_trip(depart_date, return_date):
    """True when the trip overlaps a school holiday window, or comes
    within school_holidays.TOLERANCE_DAYS (2) of one on either side —
    every relation school_holidays.nearby() can return (during, before,
    after), not just "during" (2026-09-19 tried "during" only; revised
    2026-09-20, directly requested: some people would happily take a
    couple of days of annual leave on either side of a holiday for a
    cheaper fare, so a trip landing just before or after still counts).
    No trip-length floor — a holiday trip is reasonably anywhere from a
    few days to a few weeks.

    Takes real date-like objects, matching is_weekend_trip()'s
    convention. school_holidays.nearby() itself wants ISO strings (it's
    built to read flags already loaded from JSON) — converted here rather
    than changing that function's contract, since its other caller
    (notify.py's tag rendering) uses the same during/before/after result
    to decide the tag's wording, unchanged by this."""
    result = school_holidays.nearby(
        {"depart_date": _as_date_str(depart_date), "return_date": _as_date_str(return_date)}
    )
    return result is not None


# name -> eligibility predicate. The one place both products are defined;
# sweep.py and notify.py iterate this rather than hard-coding the two
# names twice each.
PRODUCTS = {
    "weekend": is_weekend_trip,
    "holiday": is_holiday_trip,
}


def detect(sweep_date, product, config=None):
    """Return a list of flagged-deal dicts for the given sweep date and
    product ("weekend" or "holiday" — see PRODUCTS). Read-only — see
    write_flags() for persisting the result."""
    if product not in PRODUCTS:
        raise ValueError(f"unknown product {product!r}, expected one of {sorted(PRODUCTS)}")
    is_eligible = PRODUCTS[product]
    flagged_col = f"flagged_min_price_{product}"

    config = config or load_config()
    min_observations = config["detection"]["min_observations"]
    drop_pct_threshold = config["detection"]["drop_pct_threshold"]

    delta_path = storage.DELTA_DIR / f"{sweep_date.isoformat()}.parquet"
    if not delta_path.exists():
        return []  # nothing changed that night — nothing to (re-)evaluate

    tonight = pd.read_parquet(delta_path)
    if tonight.empty:
        return []

    index_df = storage.load_index()
    index_lookup = index_df.set_index(storage.KEY_COLUMNS)

    flags = []
    newly_flagged = {}  # key -> price, written back into the index below
    # Grouped by depart_month so load_full_history() reads each month's
    # data once, not once per row touched tonight.
    for depart_month, group in tonight.groupby("depart_month"):
        history = storage.load_full_history(depart_months=[depart_month])
        if history.empty:
            continue
        history = history.set_index(storage.KEY_COLUMNS).sort_index()

        for _, row in group.iterrows():
            key = _key_tuple(row)

            if key not in index_lookup.index:
                continue  # shouldn't happen — every changed row was just indexed
            obs_count = index_lookup.loc[key, "observation_count"]
            if isinstance(obs_count, pd.Series):
                obs_count = obs_count.iloc[0]
            if obs_count < min_observations:
                continue

            if not is_eligible(row["depart_date"], row["return_date"]):
                continue

            if key not in history.index:
                continue
            key_history = history.loc[[key]]
            prior = key_history[key_history["observed_at"] < row["observed_at"]]
            if prior.empty:
                continue

            baseline_min = prior["price_gbp"].min()
            baseline_median = prior["price_gbp"].median()
            tonight_price = row["price_gbp"]

            is_new_low = tonight_price <= baseline_min
            is_meaningfully_below = tonight_price <= baseline_median * (1 - drop_pct_threshold)

            # Must beat this exact flight's own last flagged price for
            # *this product*, not merely tie it — otherwise a fare that
            # just holds at its record low (e.g. a different flight
            # number now selling the same price, which still counts as
            # "changed" for storage purposes) re-flags every night it's
            # re-observed. NaN means never flagged before by this
            # product, so anything eligible clears this.
            flagged_min_price = index_lookup.loc[key, flagged_col]
            if isinstance(flagged_min_price, pd.Series):
                flagged_min_price = flagged_min_price.iloc[0]
            already_flagged_this_low = pd.notna(flagged_min_price) and tonight_price >= flagged_min_price

            if is_new_low and is_meaningfully_below and not already_flagged_this_low:
                flags.append(
                    {
                        "product": product,
                        "flagged_at": sweep_date.isoformat(),
                        "origin_airport": row["origin_airport"],
                        "destination": row["destination"],
                        "depart_date": _as_date_str(row["depart_date"]),
                        "return_date": _as_date_str(row["return_date"]),
                        "trip_type": row["trip_type"],
                        "price_gbp": float(tonight_price),
                        "prior_min_gbp": float(baseline_min),
                        "prior_median_gbp": float(baseline_median),
                        "drop_pct_vs_median": round(1 - (tonight_price / baseline_median), 3),
                        "observation_count": int(obs_count),
                        "airline": row["airline"],
                        "flight_number": row["flight_number"],
                    }
                )
                newly_flagged[key] = tonight_price

    if newly_flagged:
        _record_flagged_prices(index_df, newly_flagged, flagged_col)
        storage.save_index(index_df)

    return flags


def _record_flagged_prices(index_df, newly_flagged, flagged_col):
    """Mutate index_df in place: stamp flagged_col for every key that just
    flagged (under this product), so a future night only re-flags this
    exact flight, for this same product, if it beats that price. A
    boolean mask per key rather than a MultiIndex .loc assignment —
    simpler and avoids another dtype-alignment surprise on top of the tz
    one already found the hard way in storage.py.

    Note: relies on exact equality across KEY_COLUMNS, including
    return_date — fine while trip_type is round-trip only (PLAN.md §3),
    since NaN/NaT never equals itself and would silently fail to match if
    one-way (null return_date) keys ever entered the mix.
    """
    for key, price in newly_flagged.items():
        mask = pd.Series(True, index=index_df.index)
        for col, value in zip(storage.KEY_COLUMNS, key):
            mask &= index_df[col] == value
        index_df.loc[mask, flagged_col] = price


def write_flags(flags, sweep_date, product):
    """Write tonight's flags for this product to a dated file under its
    own product subdirectory — only if there are any. An empty result is
    valid and shouldn't create an empty file, matching write_delta()'s
    same convention (PATTERNS.md §7)."""
    if not flags:
        return None
    product_dir = FLAGS_DIR / product
    product_dir.mkdir(parents=True, exist_ok=True)
    path = product_dir / f"{sweep_date.isoformat()}.jsonl"
    with open(path, "a") as f:
        for flag in flags:
            f.write(json.dumps(flag) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--date", help="YYYY-MM-DD to evaluate. Defaults to today (UTC).")
    parser.add_argument(
        "--product",
        choices=sorted(PRODUCTS),
        help="Restrict to one product. Defaults to both.",
    )
    args = parser.parse_args()

    sweep_date = date.fromisoformat(args.date) if args.date else storage._today_utc()
    products = [args.product] if args.product else sorted(PRODUCTS)

    for product in products:
        flags = detect(sweep_date, product)
        path = write_flags(flags, sweep_date, product)

        print(f"detect[{product}]({sweep_date}): {len(flags)} flagged")
        for flag in flags:
            print(
                f"  {flag['origin_airport']}->{flag['destination']} {flag['depart_date']}: "
                f"£{flag['price_gbp']:.0f} (typically £{flag['prior_median_gbp']:.0f}, "
                f"{flag['drop_pct_vs_median'] * 100:.0f}% below, "
                f"{flag['observation_count']} nights observed)"
            )
        if path:
            print(f"  written: {path}")


if __name__ == "__main__":
    main()
