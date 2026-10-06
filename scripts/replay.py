#!/usr/bin/env python3
"""
Flight Deal Scanner — replay: "what would this have flagged?", before it
reaches anyone.

Every detection rule change so far (the 15% -> 30% -> 20% threshold, the
trip-shape rules, the 21-day cap) was judged by reading the emails it
produced *after* they went out. That was fine for one recipient; with
strangers on the list it isn't. This re-runs detection against the real
nights still on disk, under the current config or under overrides you
name, and prints what would have fired and where the rest dropped out.

Strictly read-only: it never writes flags, never saves the index, never
sends anything (detect.detect(persist=False)).

Usage:
    python scripts/replay.py                                   # current settings
    python scripts/replay.py --set detection.drop_pct_threshold=0.25
    python scripts/replay.py --set detection.max_lead_days=28 --set detection.min_observations=4
    python scripts/replay.py --nights 2                        # only the 2 newest nights

Limits, stated because they matter when reading the numbers:
- Only nights whose raw delta file still exists can be replayed — older
  ones are folded into the monthly files, which don't record which fares
  changed on which night. That is about the last 4 nights.
- It judges those nights against *today's* index: observation counts are
  as of now (slightly generous to the "seen on enough nights" gate), and a
  fare already flagged for real is blocked from flagging again (slightly
  stingy). Treat results as a preview, not a measurement.
"""
import argparse
import copy
from datetime import date

import yaml

import detect
import storage
from config import load_config

_STAGE_HEADINGS = (
    ("changed", "changed"),
    ("right_shape", "shape"),
    ("within_lead_cap", "lead"),
    ("seen_enough", "seen"),
    ("has_history", "history"),
    ("new_low", "new low"),
    ("drop_ok", "drop"),
    ("flagged", "FLAGGED"),
)


def apply_overrides(config, overrides):
    """Return a deep copy of config with each "a.b=value" applied. The
    value is parsed as YAML so 0.25, 28 and true come through typed. A name
    that doesn't already exist in the config is an error, not a new key — a
    typo'd setting would otherwise quietly replay the *current* behaviour
    and look like "that change makes no difference"."""
    cfg = copy.deepcopy(config)
    for item in overrides:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"--set expects name=value (e.g. detection.drop_pct_threshold=0.25), got {item!r}")
        *parents, leaf = key.split(".")
        node = cfg
        for part in parents:
            if not isinstance(node, dict) or part not in node:
                raise SystemExit(f"--set {key}: no such setting")
            node = node[part]
        if not isinstance(node, dict) or leaf not in node:
            raise SystemExit(f"--set {key}: no such setting")
        if isinstance(node[leaf], dict):
            raise SystemExit(f"--set {key}: that's a whole section, not a setting — name one inside it")
        node[leaf] = yaml.safe_load(raw)
    return cfg


def available_nights():
    return sorted(date.fromisoformat(f.stem) for f in storage.DELTA_DIR.glob("*.parquet"))


def replay(config, nights):
    """[(night, product, flags, funnel_counts), ...] — read-only."""
    out = []
    for night in nights:
        for product in sorted(detect.PRODUCTS):
            stats = {}
            flags = detect.detect(night, product, config=config, stats=stats, persist=False)
            out.append((night, product, flags, stats))
    return out


def _describe(flag, night):
    dep = date.fromisoformat(flag["depart_date"])
    ret = date.fromisoformat(flag["return_date"])
    return (
        f"{flag['origin_airport']}->{flag['destination']}  "
        f"{dep.strftime('%a')} {dep.day} {dep.strftime('%b')} -> {ret.strftime('%a')} {ret.day} {ret.strftime('%b')}  "
        f"£{flag['price_gbp']:.0f} ({flag['drop_pct_vs_median'] * 100:.0f}% below typical)  "
        f"departs in {(dep - night).days}d"
    )


def _totals(results):
    totals = {p: 0 for p in detect.PRODUCTS}
    for _night, product, flags, _stats in results:
        totals[product] += len(flags)
    return totals


def _settings_line(config):
    d = config["detection"]
    return (
        f"drop >= {d['drop_pct_threshold'] * 100:.0f}% below typical, departs within {d['max_lead_days']}d, "
        f"seen on >= {d['min_observations']} nights, new-low window {d['new_low_lookback_days']}d"
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="name=value",
        help="Override a config value for this replay only, e.g. detection.drop_pct_threshold=0.25. Repeatable.",
    )
    parser.add_argument("--nights", type=int, help="Replay only the N most recent nights on disk.")
    args = parser.parse_args()

    base = load_config()
    config = apply_overrides(base, args.overrides)

    nights = available_nights()
    if args.nights:
        nights = nights[-args.nights :]
    if not nights:
        raise SystemExit("No raw delta files on disk to replay.")

    print(
        f"Replaying {len(nights)} real night(s), {nights[0]} .. {nights[-1]} — read-only, "
        f"nothing written or sent."
    )
    print(f"Settings: {_settings_line(config)}")
    if args.overrides:
        print("Overrides: " + ", ".join(args.overrides))
    print()

    results = replay(config, nights)

    heading = " | ".join(h for _s, h in _STAGE_HEADINGS)
    print(f"Where fares dropped out ({heading}):")
    for night, product, flags, stats in results:
        row = " | ".join(f"{stats[s]:>5}" if h != "FLAGGED" else f"{stats[s]:>7}" for s, h in _STAGE_HEADINGS)
        print(f"  {night}  {product:<8} {row}")
        for flag in flags:
            print(f"      -> {_describe(flag, night)}")

    totals = _totals(results)
    summary = ", ".join(f"{p} {n}" for p, n in sorted(totals.items()))
    print(f"\nWould have flagged: {summary}")
    if args.overrides:
        base_totals = _totals(replay(base, nights))
        base_summary = ", ".join(f"{p} {n}" for p, n in sorted(base_totals.items()))
        print(f"Current settings flag:  {base_summary}")
    print(
        "\nA preview, not a measurement: older nights are judged against today's index "
        "(see this script's docstring)."
    )


if __name__ == "__main__":
    main()
