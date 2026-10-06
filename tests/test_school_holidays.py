"""The school-holiday calendar is hand-maintained data that decides what the
Holiday product flags. These guard the assumptions the code leans on, so
extending the calendar (it only runs to Feb 2028) can't quietly break them."""
from datetime import timedelta

import school_holidays as sh


def test_windows_are_well_formed_and_in_date_order():
    for label, start, end in sh.HOLIDAYS:
        assert label and start <= end, (label, start, end)
    starts = [s for _, s, _ in sh.HOLIDAYS]
    assert starts == sorted(starts), "keep the list chronological; nearby() returns the first hit"


def test_windows_are_far_enough_apart_that_a_trip_can_only_ever_be_near_one():
    """nearby()'s docstring says at most one holiday can match. That's only
    true while the gap between windows exceeds both tolerance zones; with
    less, a trip could be 'just after' one and 'just before' the next and
    the first listed would silently win."""
    ordered = sorted(sh.HOLIDAYS, key=lambda h: h[1])
    for (a_label, _, a_end), (b_label, b_start, _) in zip(ordered, ordered[1:]):
        gap = (b_start - a_end).days
        assert gap > 2 * sh.TOLERANCE_DAYS, f"{a_label} -> {b_label}: only {gap} days apart"


def _flag(depart, ret):
    return {"depart_date": depart.isoformat(), "return_date": ret.isoformat()}


def test_relations_at_the_exact_edges_of_october_half_term():
    label, start, end = next(h for h in sh.HOLIDAYS if h[0] == "October half-term" and h[1].year == 2026)
    day = timedelta(days=1)

    assert sh.nearby(_flag(start, start)) == (label, "during")
    assert sh.nearby(_flag(end, end)) == (label, "during")
    assert sh.nearby(_flag(start - 3 * day, start - 2 * day)) == (label, "before")
    assert sh.nearby(_flag(end + 2 * day, end + 4 * day)) == (label, "after")
    assert sh.nearby(_flag(start - 5 * day, start - 3 * day)) is None
    assert sh.nearby(_flag(end + 3 * day, end + 5 * day)) is None


def test_a_trip_that_spans_a_whole_holiday_is_during_not_before_or_after():
    label, start, end = sh.HOLIDAYS[0]
    assert sh.nearby(_flag(start - timedelta(days=10), end + timedelta(days=10))) == (label, "during")
