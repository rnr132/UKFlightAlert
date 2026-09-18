"""
IATA airline code -> name, for the digest — knowing which airline is
selling a fare is what turns "here's a cheap flight" into something
someone can actually go and book.

Offline lookup only, same rule as places.py — nothing here makes a
network call, so the no-external-calls constraint on the sweep path still
holds even though notify.py sits outside it. There is no actively
maintained pip package for airline codes the way airportsdata covers
airports (checked before building this rather than assumed absent), so
airlines_data.csv is vendored directly from OpenFlights' long-standing
open airline dataset (github.com/jpatokal/openflights/blob/master/data/
airlines.dat), filtered to active, valid-2-letter-IATA rows.

That source has real duplicate-IATA-code entries even among "active"
rows — e.g. VY matches both Vueling Airlines (Spain) and the much smaller
Formosa Airlines (Taiwan). Every code that actually appeared in this
project's real swept data was checked by hand against the route it flew
(VY on LHR->ALC is obviously Vueling, not a Taiwanese carrier); the rest
of the ~1000 rows got a best-effort tiebreak (prefer a name with no
Cargo/Domestic/Express/Regional qualifier, else the shorter name) and
haven't all been individually verified. One code (RR) was dropped
entirely — its only "active" match was a virtual/community entity, not a
real commercial carrier, so showing nothing is more honest than showing
that. Full generation notes and the real-data checks are in PLAN.md.

Regenerate airlines_data.csv from a fresh OpenFlights pull if this ever
needs updating — the filter/dedup logic isn't kept as a script here since
it's a one-off data-prep step, not something that runs regularly.
"""
import csv
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "airlines_data.csv"

# Codes found wrong or missing after checking real results — same
# incremental-fix pattern as places.py's _OVERRIDES, for whatever the
# vendored data gets wrong or lacks next.
_OVERRIDES = {
    # 2026-09-18: a real digest showed "Avialeasing Aviation Company
    # EC8299" on an LGW->KTW (Poland) route -- implausible for a Uzbek
    # cargo airline (confirmed cargo-only, Wikipedia) on an EU short-haul
    # route, so checked rather than assumed correct. OpenFlights' own
    # data has a genuine error here: Avialeasing's ICAO (TWN) matches,
    # but its real IATA code is V2, not EC (Wikipedia) -- EC actually
    # belongs to easyJet Europe (Wikipedia's "List of airline codes (E)"),
    # the Vienna-based EU subsidiary easyJet set up post-Brexit to keep
    # flying intra-EU routes, which fits an LGW->KTW fare exactly.
    "EC": "easyJet Europe",
}


def _load():
    lookup = {}
    with open(_DATA_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lookup[row["iata"]] = row["name"]
    return lookup


_AIRLINES = _load()


def resolve(code):
    """IATA 2-letter airline code -> name, or the code itself if
    unresolved — never fatal, never a guess dressed up as fact, same
    convention as places.resolve()."""
    if not code:
        return code
    return _OVERRIDES.get(code) or _AIRLINES.get(code) or code
