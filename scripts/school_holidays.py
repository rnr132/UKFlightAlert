"""
Approximate London/England school holiday windows (built 2026-09-17).

Not a per-borough calendar — there isn't one. England's ~33 London
boroughs, plus academies and free schools (which set their own dates
entirely), each publish their own term dates, typically within a few days
of one another. This is a single common-case estimate — "most likely
London school holidays", which is what was asked for — good for a rough
"does this trip touch half-term" signal on a family-facing digest, not an
authoritative source for any one specific school.

2026-27 school year below is sourced from real, currently-published
borough term dates (Bexley, Greenwich, Lambeth, Harrow, Hounslow, Tower
Hamlets — checked live, all converged on the same or near-identical
dates) rather than estimated. 2027-28 isn't published by any borough yet
this far out (publication typically runs 1-2 years ahead of the school
year), so those rows are extrapolated from the same seasonal pattern —
flagged inline below. Revisit once real 2027-28 dates are published,
likely sometime in 2027.

Each window is the full calendar gap between terms (the weekends either
side of a half-term included, not just the Mon-Fri school-closed days) —
the more honest definition of "on holiday", and the more generous edge
for the +/-2-day tolerance notify.py applies on top when matching a trip
against these.
"""
from datetime import date, timedelta

# (label, start, end)
HOLIDAYS = [
    # --- 2026-27 school year: sourced live, see module docstring ---
    ("October half-term", date(2026, 10, 24), date(2026, 11, 1)),
    ("Christmas holidays", date(2026, 12, 19), date(2027, 1, 3)),
    ("February half-term", date(2027, 2, 13), date(2027, 2, 21)),
    ("Easter holidays", date(2027, 3, 26), date(2027, 4, 11)),
    ("May half-term", date(2027, 5, 29), date(2027, 6, 6)),
    ("Summer holidays", date(2027, 7, 22), date(2027, 8, 31)),
    # --- 2027-28 school year: extrapolated, not yet published anywhere — see docstring ---
    ("October half-term", date(2027, 10, 23), date(2027, 10, 31)),
    ("Christmas holidays", date(2027, 12, 18), date(2028, 1, 2)),
    ("February half-term", date(2028, 2, 12), date(2028, 2, 20)),
]

TOLERANCE_DAYS = 2


def nearby(flag):
    """(label, relation) for the London school holiday this flag's trip
    relates to, within TOLERANCE_DAYS of it — or None if it's nowhere
    near any of them. `relation` is one of:

      "during" — the trip's own [depart_date, return_date] genuinely
                 overlaps the holiday's real (unpadded) window
      "before" — the trip ends before the holiday starts, but within
                 TOLERANCE_DAYS of its start
      "after"  — the trip starts after the holiday ends, but within
                 TOLERANCE_DAYS of its end

    "during" is checked against the *real* window, not the padded one —
    the padding only decides whether a non-overlapping trip is close
    enough to mention at all, and shouldn't blur into a false "during".

    Takes the flag dict directly, matching notify.py's _trip_nights()
    convention, rather than pre-parsed dates. Windows don't overlap each
    other, so at most one holiday can ever match — first (only) hit wins."""
    depart = date.fromisoformat(flag["depart_date"])
    ret = date.fromisoformat(flag["return_date"])
    for label, start, end in HOLIDAYS:
        buf_start = start - timedelta(days=TOLERANCE_DAYS)
        buf_end = end + timedelta(days=TOLERANCE_DAYS)
        if depart > buf_end or ret < buf_start:
            continue  # outside the tolerance window entirely
        if depart <= end and ret >= start:
            return label, "during"
        if ret < start:
            return label, "before"
        return label, "after"  # only remaining case: depart > end
    return None
