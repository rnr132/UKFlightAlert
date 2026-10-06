#!/usr/bin/env python3
"""
Flight Deal Scanner — nightly digest email (the delivery half of Phase 2).

Two hyper-specialized products, one combined email (2026-09-19 pivot —
see detect.py's module docstring for the full reasoning): a Weekend
Deals section and a Holiday Deals section, each built from that
product's own data/flags/<product>/YYYY-MM-DD.jsonl, sharing one send so
today's one recipient list doesn't need per-user subscriptions built to
support two products that don't exist yet independently.

Nightly, not weekly — the old "weekly signal, not real-time alert"
framing was designed around a generic route-is-cheap-for-the-season
alert. A weekend-trip deal is inherently short-lead-time; batching it for
up to six days before sending ate directly into the window that's the
whole premise of that product. Moving to nightly doesn't change the
*data's* staleness (still a 2-7-day-old cache, same as always) — it just
stops adding avoidable delivery lag on top of unavoidable data lag. Skips
silently on a quiet night: no flags clearing the bar in either product,
no email — the same "empty result is valid" convention as write_delta()
and write_flags().

Checks tonight's flags only, not a rolling window (see _load_tonight_flags())
— a missed run is replayed by hand via workflow_dispatch, the same
established pattern this project already relies on for a missed sweep,
rather than a wider window that risks re-sending an already-delivered
flag the next time it runs.

No LLM calls anywhere, in either version — pure string/template
building, matching the brief's "no LLM calls in the sweep path"
constraint. HTML version: table-based layout, inline styles, no external
images or fonts (2026-09-17), with a plain-text alternative in the same
message, not HTML alone.

Each recipient gets their own message (never one message with everyone on
the To: line), with a Reply-To and List-Unsubscribe pointing at the inbox
in the REPLY_TO secret, and an "x of y delivered" record in the notify
heartbeat. A re-run the same day only sends fares not already emailed.

Usage:
    python scripts/notify.py                  # send if either product has something new tonight
    python scripts/notify.py --dry-run        # print the text version, write an HTML preview file, send nothing
    python scripts/notify.py --dry-run --date 2026-10-02   # preview a past night that had flags
    python scripts/notify.py --test you@x.com # ONE real email to a single address, ignoring the real recipient list — for reviewing the format before it ever reaches anyone else
    python scripts/notify.py --resend         # send even fares already emailed earlier for this date
"""
import argparse
import html
import json
import os
import re
import smtplib
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import airlines
import detect
import places
import school_holidays
from config import REPO_ROOT, load_config

NOTIFY_HEARTBEAT_PATH = REPO_ROOT / "data" / "notify_heartbeat.jsonl"

PREVIEW_PATH = REPO_ROOT / "scratch" / "digest_preview.html"


def _load_tonight_flags(as_of, product):
    """Tonight's flags for one product, not a rolling window — see the
    module docstring for why nightly delivery means "new since last
    night" is just "tonight", with no multi-day collapsing needed."""
    path = detect.FLAGS_DIR / product / f"{as_of.isoformat()}.jsonl"
    if not path.exists():
        return []
    flags = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                flags.append(json.loads(line))
    return flags


def _trip_nights(flag):
    return (date.fromisoformat(flag["return_date"]) - date.fromisoformat(flag["depart_date"])).days


def _fmt_date(iso_str):
    """2026-10-01 -> 1-Oct-26. Built by hand rather than strftime('%-d')
    since the no-leading-zero directive isn't portable across platforms."""
    d = date.fromisoformat(iso_str)
    return f"{d.day}-{d.strftime('%b')}-{d.strftime('%y')}"


def _date_range_label(depart_iso, return_iso):
    """'1-8 Oct 26' when both dates share a month and year — how a person
    actually writes a date range, not two full dates stitched together
    with "to". Falls back to '18-Dec-26 to 1-Jan-27' (both full dates)
    whenever month or year differs, so a range that crosses either is
    never ambiguous about which date belongs to which month."""
    d = date.fromisoformat(depart_iso)
    r = date.fromisoformat(return_iso)
    if (d.year, d.month) == (r.year, r.month):
        return f"{d.day}–{r.day} {d.strftime('%b')} {d.strftime('%y')}"
    return f"{_fmt_date(depart_iso)} to {_fmt_date(return_iso)}"


def _eligible_fares(flags, min_drop_pct):
    """Filter -> collapse -> enrich. Filters to fares that dropped at
    least `min_drop_pct` — trip-shape/holiday-window eligibility is
    already baked into which file a flag lives in, via detect.py's
    per-product gate (2026-09-19), so there's nothing left to re-check
    here on that front. Collapses repeat flags of the same itinerary to
    the latest (which by the re-flag rule in detect.py is also the
    lowest — mostly a no-op now that each file only ever holds one
    night's flags, kept as cheap insurance rather than assumed
    unnecessary), then resolves each surviving fare's place and
    school-holiday proximity once.

    Returns a flat list of enriched fare dicts (each carrying _city,
    _country, _continent, _holiday), or None if nothing clears the bar."""
    eligible = [f for f in flags if f["drop_pct_vs_median"] >= min_drop_pct]
    if not eligible:
        return None

    latest = {}
    for f in eligible:
        k = (f["origin_airport"], f["destination"], f["depart_date"], f["return_date"])
        if k not in latest or f["flagged_at"] > latest[k]["flagged_at"]:
            latest[k] = f

    enriched = []
    for f in latest.values():
        city, country, continent = places.resolve(f["destination"])
        enriched.append(
            {
                **f,
                "_city": city,
                "_country": country,
                "_continent": continent,
                "_holiday": school_holidays.nearby(f),
            }
        )
    return enriched


def _prepare_section(flags, min_drop_pct):
    """_eligible_fares(), grouped region -> destination -> [fares].
    Returns (tree, count) where count is the number of unique itineraries
    shown, not len(flags) — those differ whenever a threshold change
    means an old flag in the file no longer clears the current bar.
    Returns (None, 0) if nothing clears it."""
    enriched = _eligible_fares(flags, min_drop_pct)
    if enriched is None:
        return None, 0
    tree = defaultdict(lambda: defaultdict(list))
    for f in enriched:
        tree[f["_continent"]][f["destination"]].append(f)
    return tree, len(enriched)


def _best(fares):
    return max(x["drop_pct_vs_median"] for x in fares)


def _sorted_continents(tree):
    return sorted(tree, key=lambda c: max(_best(v) for v in tree[c].values()), reverse=True)


def _sorted_destinations(tree, continent):
    return sorted(tree[continent], key=lambda d: _best(tree[continent][d]), reverse=True)


def _place_label(head, dcode):
    """'Barcelona, Spain' — or just the raw code when places.resolve()
    couldn't identify it (PLAN.md: unknown codes fall through to 'Other'
    rather than crashing), so an unresolved destination never renders as
    a bare, confusing ' (XYZ)'."""
    place = ", ".join(p for p in (head["_city"], head["_country"]) if p)
    return place or dcode


_HOLIDAY_RELATION_PHRASING = {
    "during": "During",
    "before": "Just before",
    "after": "Just after",
}


def _holiday_phrase(holiday):
    """'During October half-term' / 'Just before Christmas holidays' /
    'Just after February half-term' — human phrasing for a
    school_holidays.nearby() result, shared by both renderers so the
    wording can't drift between the text and HTML versions. `holiday` is
    the (label, relation) tuple, or None (caller checks truthiness
    first). Still shown as an informational tag in the Weekend Deals
    section too — a weekend trip that happens to land during half-term is
    worth knowing about even though it's the Holiday product that gates
    on this, not the Weekend one."""
    label, relation = holiday
    return f"{_HOLIDAY_RELATION_PHRASING[relation]} {label}"


def _airline_label(f):
    """'Vueling VY1234' — the name someone would actually search for to
    book this exact flight, not just the 2-letter code. Falls back to the
    raw code if airlines.resolve() doesn't know it (never crashes, never
    hides the flight number even when the name is unknown). None if the
    flag is missing either field, so callers can check truthiness and
    render nothing rather than crash."""
    airline = f.get("airline")
    flight_number = f.get("flight_number")
    if not airline or not flight_number:
        return None
    return f"{airlines.resolve(airline)} {airline}{flight_number}"


def _booking_url(f):
    """A clickable link to search this exact fare — the affiliate-tracked
    partner_url when scripts/booking_links.py's API conversion succeeded,
    falling back to the plain (non-affiliate) search_url otherwise, so a
    recipient still gets a working link even on a night the conversion API
    had a problem. None for a flag missing both fields, so callers can
    check truthiness and render nothing rather than crash."""
    return f.get("partner_url") or f.get("search_url")


_PRODUCT_COPY = {
    "weekend": {
        "title": "Weekend Deals",
        "emoji": "\U0001F389",
        "intro": "flights that work as a quick weekend trip",
    },
    "holiday": {
        "title": "Holiday Deals",
        "emoji": "\U0001F3D6",
        "intro": "flights during, or within a couple of days of, an upcoming school holiday",
    },
}


def _render_text_section(product, flags, min_drop_pct):
    """Lines for one product's section of the digest, or (None, 0) if
    nothing in flags clears the bar tonight."""
    tree, count = _prepare_section(flags, min_drop_pct)
    if tree is None:
        return None, 0

    copy = _PRODUCT_COPY[product]
    pct_bar = round(min_drop_pct * 100)
    fare_word = "fare" if count == 1 else "fares"
    lines = [
        copy["title"].upper(),
        "=" * len(copy["title"]),
        f"{count} {fare_word} at least {pct_bar}% below their recent typical price — {copy['intro']}.",
        "",
    ]
    for continent in _sorted_continents(tree):
        lines.append(continent.upper())
        lines.append("-" * len(continent))
        for dcode in _sorted_destinations(tree, continent):
            fares = sorted(tree[continent][dcode], key=lambda x: x["drop_pct_vs_median"], reverse=True)
            lines.append(f"  {_place_label(fares[0], dcode)} ({dcode})")
            for f in fares:
                pct = round(f["drop_pct_vs_median"] * 100)
                nights = _trip_nights(f)
                lines.append(
                    f"    {f['origin_airport']}  "
                    f"GBP {f['price_gbp']:.0f}  "
                    f"(typically GBP {f['prior_median_gbp']:.0f}, {pct}% below)"
                )
                airline_label = _airline_label(f)
                if airline_label:
                    lines.append(f"      {airline_label}")
                lines.append(
                    f"      {_date_range_label(f['depart_date'], f['return_date'])}  "
                    f"({nights} night{'s' if nights != 1 else ''})  "
                    f"flagged {_fmt_date(f['flagged_at'])}"
                )
                if f["_holiday"]:
                    lines.append(f"      ★ {_holiday_phrase(f['_holiday'])}")
                booking_url = _booking_url(f)
                if booking_url:
                    lines.append(f"      Search this fare: {booking_url}")
            lines.append("")
        lines.append("")
    return lines, count


def build_digest_text(weekend_flags, holiday_flags, as_of, min_drop_pct, can_unsubscribe=False):
    """Plain-text digest body combining both products' sections, or
    (None, 0, 0) if neither has anything to say tonight. This is the
    multipart/alternative fallback for clients/screen readers that don't
    render HTML — not a lesser version, a different one, so it's built
    directly rather than stripped-down from the HTML.

    can_unsubscribe adds the "reply to stop" line — only when a Reply-To
    inbox is actually configured, since promising that with nowhere for
    replies to land would be worse than saying nothing."""
    weekend_lines, weekend_count = _render_text_section("weekend", weekend_flags, min_drop_pct)
    holiday_lines, holiday_count = _render_text_section("holiday", holiday_flags, min_drop_pct)
    if weekend_lines is None and holiday_lines is None:
        return None, 0, 0

    lines = [f"London Flight Deals — {_fmt_date(as_of.isoformat())}", ""]
    if weekend_lines:
        lines += weekend_lines
    if holiday_lines:
        lines += holiday_lines
    lines.append(
        "This is a nightly signal, not a real-time alert — the underlying "
        "data can be a few days old. If a route above still looks good, "
        "worth checking live before booking."
    )
    if can_unsubscribe:
        lines.append("")
        lines.append(_UNSUBSCRIBE_LINE)
    return "\n".join(lines), weekend_count, holiday_count


# ---------------------------------------------------------------------------
# HTML rendering (2026-09-17, restructured into sections 2026-09-19). Table-
# based layout with inline styles on every structurally-important element —
# not because <style> blocks never work in email, but because inline is the
# one thing that renders the same everywhere from Gmail to Outlook to a
# phone's mail app. No remote images or web fonts: nothing for a client's
# image-blocking to break, nothing to fail to load — a system font stack
# renders natively on every platform anyway.
# ---------------------------------------------------------------------------

_FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _badge(pct):
    """(background, text colour, prefix) for the drop-% pill — three flat
    tiers so colour gives a rough-magnitude signal at a glance, without a
    gradient's extra state to keep consistent. Bigger drop, warmer colour."""
    if pct >= 0.50:
        return "#fef3c7", "#92400e", "\U0001F525 "  # amber — a standout drop
    if pct >= 0.35:
        return "#dcfce7", "#166534", ""  # green
    return "#dbeafe", "#1e40af", ""  # blue — base tier, still cleared the bar


def _esc(s):
    return html.escape(str(s), quote=True)


def _render_fare_row(f, is_last):
    pct = f["drop_pct_vs_median"]
    bg, fg, prefix = _badge(pct)
    nights = _trip_nights(f)
    border = "" if is_last else "border-bottom:1px solid #e2e8f0;"
    return f"""
      <tr><td style="padding:{'0' if is_last else '0 0 12px'};{'' if is_last else 'padding-bottom:12px;'}">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{border}padding-top:12px;">
          <tr>
            <td style="font-size:20px;font-weight:700;color:#0f172a;font-family:{_FONT_STACK};">
              &#163;{f['price_gbp']:.0f}
              <span style="font-size:12px;font-weight:400;color:#94a3b8;text-decoration:line-through;margin-left:6px;">&#163;{f['prior_median_gbp']:.0f}</span>
            </td>
            <td align="right" style="white-space:nowrap;">
              <span style="display:inline-block;background:{bg};color:{fg};font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;font-family:{_FONT_STACK};">{prefix}{round(pct * 100)}% off</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="font-size:13px;color:#64748b;padding-top:4px;font-family:{_FONT_STACK};">
              {_esc(f['origin_airport'])} &middot; {_date_range_label(f['depart_date'], f['return_date'])} &middot; {nights} night{'s' if nights != 1 else ''}
            </td>
          </tr>{_render_airline_row(f)}{_render_holiday_tag(f['_holiday'])}{_render_booking_link_row(f)}
        </table>
      </td></tr>"""


def _render_airline_row(f):
    """A muted line under the route/dates line naming the airline and
    flight number. Empty string when _airline_label() returns None, same
    "splice in unconditionally" convention as _render_holiday_tag()."""
    label = _airline_label(f)
    if not label:
        return ""
    return f"""
          <tr>
            <td colspan="2" style="font-size:13px;color:#64748b;padding-top:2px;font-family:{_FONT_STACK};">
              {_esc(label)}
            </td>
          </tr>"""


def _render_holiday_tag(holiday):
    """A small violet tag under the route/dates line when the trip falls
    within +/-2 days of a London school holiday (school_holidays.nearby())
    — deliberately a different colour from the drop-% badge above it, so
    it reads as a different *kind* of signal (timing, not price). Empty
    string when there's no match, so the caller can always splice this in
    unconditionally."""
    if not holiday:
        return ""
    return f"""
          <tr>
            <td colspan="2" style="padding-top:6px;">
              <span style="display:inline-block;background:#f3e8ff;color:#6b21a8;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;font-family:{_FONT_STACK};">&#127890; {_esc(_holiday_phrase(holiday))}</span>
            </td>
          </tr>"""


def _render_booking_link_row(f):
    """A text link under the fare's other detail rows so a recipient can go
    straight from "this looks cheap" to a real search. Empty string when
    there's no link at all, same "splice in unconditionally" convention
    as _render_airline_row()/_render_holiday_tag()."""
    url = _booking_url(f)
    if not url:
        return ""
    return f"""
          <tr>
            <td colspan="2" style="padding-top:8px;">
              <a href="{_esc(url)}" style="font-size:13px;font-weight:700;color:#1e3a8a;text-decoration:none;font-family:{_FONT_STACK};">Search this fare &rarr;</a>
            </td>
          </tr>"""


def _render_destination_card(dcode, fares):
    head = fares[0]
    place = _place_label(head, dcode)
    rows = "".join(
        _render_fare_row(f, is_last=(i == len(fares) - 1)) for i, f in enumerate(fares)
    )
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #eef2f6;border-radius:10px;margin-bottom:12px;">
      <tr><td style="padding:16px 18px;">
        <div style="font-size:16px;font-weight:700;color:#0f172a;font-family:{_FONT_STACK};">
          {_esc(place)} <span style="font-weight:400;color:#94a3b8;font-size:13px;">{_esc(dcode)}</span>
        </div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}
        </table>
      </td></tr>
    </table>"""


def _render_continent_section(continent, tree):
    cards = "".join(
        _render_destination_card(dcode, sorted(tree[continent][dcode], key=lambda x: x["drop_pct_vs_median"], reverse=True))
        for dcode in _sorted_destinations(tree, continent)
    )
    return f"""
        <tr><td style="padding:4px 24px 0;">
          <p style="margin:20px 0 12px;font-size:12px;font-weight:700;letter-spacing:0.08em;color:#64748b;text-transform:uppercase;border-bottom:1px solid #e2e8f0;padding-bottom:8px;font-family:{_FONT_STACK};">{_esc(continent)}</p>
          {cards}
        </td></tr>"""


def _render_html_section(product, flags, min_drop_pct, is_first):
    """The full HTML block for one product's section (title, intro,
    continent/destination cards), or (None, 0) if nothing clears the bar
    tonight. `is_first` drops the top divider on whichever section
    happens to render first, so a night with only one product's deals
    doesn't show a stray rule above it."""
    tree, count = _prepare_section(flags, min_drop_pct)
    if tree is None:
        return None, 0

    copy = _PRODUCT_COPY[product]
    pct_bar = round(min_drop_pct * 100)
    fare_word = "fare" if count == 1 else "fares"
    sections = "".join(_render_continent_section(c, tree) for c in _sorted_continents(tree))
    border = "" if is_first else "border-top:1px solid #e2e8f0;"
    header = f"""
        <tr><td style="background:#ffffff;padding:24px 24px 4px;{border}">
          <p style="margin:0 0 4px;font-size:15px;font-weight:700;color:#0f172a;font-family:{_FONT_STACK};">{copy['emoji']} {_esc(copy['title'])}</p>
          <p style="margin:0;font-size:14px;color:#334155;line-height:1.5;font-family:{_FONT_STACK};">
            <strong>{count} {fare_word}</strong> at least <strong>{pct_bar}%</strong> below their recent typical price &mdash; {_esc(copy['intro'])}.
          </p>
        </td></tr>"""
    return header + sections, count


def build_digest_html(weekend_flags, holiday_flags, as_of, min_drop_pct, can_unsubscribe=False):
    """HTML digest body (a complete standalone document) combining both
    products' sections, or (None, 0, 0) if neither has anything to say
    tonight — same eligibility/grouping/sort as the text version, via the
    same _prepare_section() so the two can never disagree about which
    fares qualify. can_unsubscribe: see build_digest_text()."""
    sections_html = []
    counts = {}
    for product in ("weekend", "holiday"):
        flags = weekend_flags if product == "weekend" else holiday_flags
        section, count = _render_html_section(product, flags, min_drop_pct, is_first=not sections_html)
        if section:
            sections_html.append(section)
        counts[product] = count

    if not sections_html:
        return None, 0, 0

    weekend_count, holiday_count = counts["weekend"], counts["holiday"]
    total = weekend_count + holiday_count
    fare_word = "fare" if total == 1 else "fares"
    date_label = _fmt_date(as_of.isoformat())
    sections = "".join(sections_html)
    unsubscribe_html = f"<br><br>{_esc(_UNSUBSCRIBE_LINE)}" if can_unsubscribe else ""

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>London Flight Deals</title>
<style>
  body {{ margin:0; padding:0; background:#f1f5f9; }}
  a {{ color:#1e3a8a; }}
  table {{ border-collapse:collapse; }}
</style>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
    {total} {fare_word} tonight &mdash; {weekend_count} weekend, {holiday_count} holiday.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;">
        <tr><td style="background:#0f172a;padding:28px 24px;border-radius:12px 12px 0 0;">
          <div style="color:#ffffff;font-size:20px;font-weight:700;font-family:{_FONT_STACK};">&#9992;&#65039; London Flight Deals</div>
          <div style="color:#94a3b8;font-size:13px;padding-top:6px;font-family:{_FONT_STACK};">{date_label}</div>
        </td></tr>
        <tr><td style="background:#ffffff;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{sections}
          </table>
        </td></tr>
        <tr><td style="background:#ffffff;padding:8px 24px 28px;border-radius:0 0 12px 12px;">
          <p style="margin:20px 0 0;font-size:12px;color:#94a3b8;line-height:1.6;border-top:1px solid #e2e8f0;padding-top:16px;font-family:{_FONT_STACK};">
            This is a nightly signal, not a real-time alert &mdash; the underlying data can be a few days old. If a route above still looks good, worth checking live before booking.{unsubscribe_html}
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return html_doc, weekend_count, holiday_count


# One address, no whitespace/commas/semicolons/angle brackets/quotes. Not a
# full RFC 5322 parser — just enough that a pasted-in value can't smuggle in
# a second address or a header line break, since both the recipient list
# (copied by hand out of a Google Sheet) and REPLY_TO end up in headers.
_ADDRESS_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")

# Resend allows 2 requests/second by default; stay comfortably under it.
_SEND_INTERVAL_SECONDS = 0.6

_UNSUBSCRIBE_LINE = "Don't want these emails? Just reply to this one and say stop."


def _valid_address(addr):
    return bool(_ADDRESS_RE.match(addr))


def _load_recipients(config):
    """NOTIFY_RECIPIENTS -> a clean list: malformed entries and
    (case-insensitive) duplicates dropped. The public signup page makes
    both likely — someone signs up twice, or a stray space/quote comes
    along when an address is pasted out of the Sheet. Counts are printed,
    addresses never are: Actions logs on a public repo are world-readable."""
    raw = os.environ.get(config["notify"]["recipients_env_var"], "")
    seen, recipients = set(), []
    malformed = duplicates = 0
    for entry in raw.split(","):
        addr = entry.strip()
        if not addr:
            continue
        if not _valid_address(addr):
            malformed += 1
            continue
        if addr.lower() in seen:
            duplicates += 1
            continue
        seen.add(addr.lower())
        recipients.append(addr)
    if malformed or duplicates:
        print(
            f"notify: recipient list cleanup — skipped {malformed} malformed "
            f"and {duplicates} duplicate entries (addresses not printed)."
        )
    return recipients


def _load_reply_to(config):
    """The inbox replies (and unsubscribe requests) go to, from the env var
    named in notify.reply_to_env_var — a secret, not config, because it's a
    personal address and this repo is public. None if unset or malformed.
    The sending domain has no MX record (flightalert.rohit-nair.com
    CNAMEs to Vercel, and a CNAME can't coexist with one), so without this
    a reply to londondeals@... has nowhere to land."""
    var = config["notify"].get("reply_to_env_var")
    raw = os.environ.get(var, "").strip() if var else ""
    if not raw:
        return None
    if not _valid_address(raw):
        print(f"notify: {var} is set but isn't a single valid email address — ignoring it (value not printed).")
        return None
    return raw


def _from_parts(config):
    from_address = config["notify"]["from_address"]
    from_name = config["notify"].get("from_name")
    # formataddr, not an f-string, so a name with a space (or anything
    # that needs quoting/escaping) always produces a valid header — the
    # bare address is what still goes to sendmail() as the SMTP envelope
    # sender, which is a separate thing from this display name.
    from_header = formataddr((from_name, from_address)) if from_name else from_address
    return from_address, from_header


@contextmanager
def _smtp_session(config):
    # Resend's SMTP model splits "login identity" from "sender identity"
    # (2026-09-17 — see PLAN.md's delivery section): smtp_username is a
    # fixed literal, not a secret, so it lives in config/sweep.yaml like
    # smtp_host/smtp_port; from_address is the visible sender on every
    # email regardless, so it's plain config too. Only the API key is a
    # secret.
    password = os.environ.get(config["notify"]["password_env_var"])
    if not password:
        raise RuntimeError(
            f"{config['notify']['password_env_var']} not set. Put it in "
            f".env locally (see .env.example) or as a GitHub Secret for the "
            f"workflow. Failing here, at startup, rather than deep inside "
            f"an SMTP call."
        )
    with smtplib.SMTP(config["notify"]["smtp_host"], config["notify"]["smtp_port"]) as server:
        server.starttls()
        server.login(config["notify"]["smtp_username"], password)
        yield server


def _build_message(config, subject, text_body, html_body, to_address, reply_to=None):
    """One message addressed to exactly one person. html_body=None gives a
    plain-text-only message (the owner report); otherwise
    multipart/alternative, text first then HTML: RFC 2046 has the client
    render the *last* part it understands, so this prefers HTML where
    supported and falls back to plain text everywhere else (older
    clients, screen readers, "always show plain text" settings) rather
    than sending HTML-only and leaving those with nothing readable."""
    _, from_header = _from_parts(config)
    if html_body is None:
        msg = MIMEText(text_body, "plain", "utf-8")
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    msg["Subject"] = subject
    msg["From"] = from_header
    msg["To"] = to_address
    if reply_to:
        msg["Reply-To"] = reply_to
        # mailto form only: a one-click https form needs a live endpoint,
        # and the signup page is static. Gmail/Apple Mail still surface
        # this as an "Unsubscribe" control.
        msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=Unsubscribe>"
    return msg


def send_email(config, subject, text_body, html_body, recipients, reply_to=None):
    """One separate message per recipient, never one message with everyone
    on the To: line — that put every address in front of every other
    recipient (found 2026-10-06, after the public signup page started
    adding strangers alongside family). A failure for one recipient is
    counted and the rest still go out; the caller decides what to do about
    a nonzero `failed`.

    Failure details are reduced to kinds and SMTP codes on purpose:
    smtplib's own exception text includes the rejected address, and this
    runs in a public repo's Actions log.

    Returns {"sent": n, "failed": m, "failures": {kind: count}}."""
    from_address, _ = _from_parts(config)
    sent = 0
    failures = Counter()
    with _smtp_session(config) as server:
        for i, addr in enumerate(recipients):
            if i:
                time.sleep(_SEND_INTERVAL_SECONDS)
            msg = _build_message(config, subject, text_body, html_body, addr, reply_to)
            try:
                server.sendmail(from_address, [addr], msg.as_string())
                sent += 1
            except smtplib.SMTPRecipientsRefused as e:
                codes = sorted({code for code, _msg in e.recipients.values()})
                failures[f"recipient refused ({', '.join(map(str, codes))})"] += 1
            except smtplib.SMTPResponseException as e:
                failures[f"SMTP {e.smtp_code}"] += 1
            except smtplib.SMTPServerDisconnected:
                failures["connection lost"] += len(recipients) - i
                break
            except smtplib.SMTPException as e:
                failures[type(e).__name__] += 1
            except OSError as e:
                failures[f"connection error ({type(e).__name__})"] += len(recipients) - i
                break
    return {"sent": sent, "failed": sum(failures.values()), "failures": dict(failures)}


# ---------------------------------------------------------------------------
# Observability (2026-09-17), added the same day notify.py was wired into
# the nightly workflow. sweep.py has carried this exact pattern since Phase
# 1 (PATTERNS.md §4.4's fix, applied from day one) — mirrored here, not
# reinvented. append_heartbeat()/check_staleness() live in sweep.py already;
# this is their notify.py counterpart, not a generalised shared version.
# ---------------------------------------------------------------------------


def append_notify_heartbeat(record):
    """One JSON line per *real* invocation — not --dry-run or --test,
    which are manual/exploratory, not the production cadence this exists
    to watch. Includes skipped nights (nothing cleared the bar in either
    product): a genuinely quiet night still appends a line, same
    convention as sweep.py's own heartbeat, so what this protects against
    is "the step stopped running", not "there was nothing to send"."""
    NOTIFY_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTIFY_HEARTBEAT_PATH, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def check_notify_staleness(config, as_of=None):
    """Warn loudly, at startup, if notify.py's last real run is older
    than expected. Reuses monitoring.staleness_warning_hours — the same
    threshold sweep.py already checks against, since both run on the same
    nightly cadence now."""
    as_of = as_of or datetime.now(timezone.utc)
    threshold = config.get("monitoring", {}).get("staleness_warning_hours", 36)

    if not NOTIFY_HEARTBEAT_PATH.exists():
        print("notify: no heartbeat log yet — this looks like the first run.")
        return
    with open(NOTIFY_HEARTBEAT_PATH) as f:
        lines = [line for line in f if line.strip()]
    if not lines:
        print("notify: heartbeat log exists but is empty — treating as first run.")
        return

    last = json.loads(lines[-1])
    last_run_at = datetime.fromisoformat(last["run_at"])
    gap_hours = (as_of - last_run_at).total_seconds() / 3600.0
    if gap_hours > threshold:
        print(
            f"*** WARNING: notify.py's last real run was {gap_hours:.1f}h ago, "
            f"over the {threshold}h threshold. The digest step may have been "
            f"skipped or failed — check the Actions tab. ***"
        )


def _notify_heartbeat_record(as_of, sent, reason, weekend_count, holiday_count, **extra):
    record = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "sent": sent,
        "reason": reason,
        "weekend_count": weekend_count,
        "holiday_count": holiday_count,
    }
    record.update({k: v for k, v in extra.items() if v is not None})
    return record


def _flag_id(product, f):
    """Identity of one alerted fare: itinerary *and* price, so a same-day
    re-flag of the same trip at a lower price counts as new (worth another
    email) while an identical one doesn't. No personal data in it, so it's
    safe to record in the committed heartbeat."""
    return "|".join(
        str(x)
        for x in (product, f["origin_airport"], f["destination"], f["depart_date"], f["return_date"], f["price_gbp"])
    )


def _already_sent_flag_ids(as_of):
    """(ids, everything_sent): which fares an earlier *real* run for this
    same date already emailed, read back from the notify heartbeat.

    Without this, re-running the workflow the same day (the documented way
    to replay a missed night, PATTERNS.md) re-emailed every recipient the
    identical digest, and per-recipient sending makes a partial-failure
    retry more likely, not less. A same-day re-run that finds *new* flags
    still sends those — only already-sent fares are held back.

    A record from before sent_flag_ids existed (sent=True, no ids) can't
    say which fares went out, so it's treated as "everything in that
    day's file" — the safe direction for the one night of transition."""
    ids, everything = set(), False
    if not NOTIFY_HEARTBEAT_PATH.exists():
        return ids, everything
    with open(NOTIFY_HEARTBEAT_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("as_of") != as_of.isoformat() or not rec.get("sent"):
                continue
            if "sent_flag_ids" in rec:
                ids.update(rec["sent_flag_ids"])
            else:
                everything = True
    return ids, everything


def run(config=None, as_of=None, dry_run=False, test_address=None, resend=False):
    config = config or load_config()
    as_of = as_of or datetime.now(timezone.utc).date()
    # --dry-run/--test are manual and exploratory; only the unattended
    # path (what the nightly workflow actually calls) gets a heartbeat
    # entry, a staleness check, or the already-sent filter below.
    is_real_run = not dry_run and not test_address
    if is_real_run:
        check_notify_staleness(config)

    min_drop_pct = config["detection"]["drop_pct_threshold"]
    weekend_flags = _load_tonight_flags(as_of, "weekend")
    holiday_flags = _load_tonight_flags(as_of, "holiday")

    held_back = 0
    if is_real_run and not resend:
        sent_ids, everything_sent = _already_sent_flag_ids(as_of)
        before = len(weekend_flags) + len(holiday_flags)
        if everything_sent:
            weekend_flags, holiday_flags = [], []
        else:
            weekend_flags = [f for f in weekend_flags if _flag_id("weekend", f) not in sent_ids]
            holiday_flags = [f for f in holiday_flags if _flag_id("holiday", f) not in sent_ids]
        held_back = before - len(weekend_flags) - len(holiday_flags)

    reply_to = _load_reply_to(config)

    text_body, weekend_count, holiday_count = build_digest_text(
        weekend_flags, holiday_flags, as_of, min_drop_pct, can_unsubscribe=bool(reply_to)
    )
    if text_body is None:
        if held_back:
            reason = "already_sent"
            print(f"notify: {held_back} flag(s) for {as_of} were already emailed earlier today — nothing new to send (--resend to send them again)")
        else:
            reason = "no_flags"
            print("notify: nothing over the drop threshold tonight — nothing to send")
        if is_real_run:
            append_notify_heartbeat(_notify_heartbeat_record(as_of, False, reason, 0, 0))
        return {"sent": False, "reason": reason, "weekend_count": 0, "holiday_count": 0}

    html_body, _, _ = build_digest_html(
        weekend_flags, holiday_flags, as_of, min_drop_pct, can_unsubscribe=bool(reply_to)
    )

    if dry_run:
        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(html_body, encoding="utf-8")
        n_recipients = len(_load_recipients(config))
        print("notify: --dry-run, would send (text version):\n")
        print(text_body)
        print(f"\nnotify: HTML version written to {PREVIEW_PATH} for visual review")
        print(
            f"notify: would go out as {n_recipients} separate message(s), one per recipient "
            f"(addresses not printed); Reply-To/unsubscribe line: {'on' if reply_to else 'OFF — REPLY_TO not set'}"
        )
        return {"sent": False, "reason": "dry_run", "weekend_count": weekend_count, "holiday_count": holiday_count}

    parts = []
    if weekend_count:
        parts.append(f"{weekend_count} weekend")
    if holiday_count:
        parts.append(f"{holiday_count} holiday")
    total = weekend_count + holiday_count
    deal_word = "deal" if total == 1 else "deals"
    subject = f"London Flight Deals: {', '.join(parts)} {deal_word}"

    if test_address:
        if not _valid_address(test_address):
            raise RuntimeError("--test needs a single valid email address.")
        print("notify: sending ONE test email (not the real recipient list)")
        result = send_email(config, subject, text_body, html_body, [test_address], reply_to)
        if result["failed"]:
            raise RuntimeError(f"notify: the test send failed ({result['failures']}).")
        return {
            "sent": True,
            "weekend_count": weekend_count,
            "holiday_count": holiday_count,
            "test": True,
        }

    recipients = _load_recipients(config)
    if not recipients:
        raise RuntimeError(
            f"{config['notify']['recipients_env_var']} is not set or empty "
            f"— nothing to send to."
        )
    if not reply_to:
        print(
            f"notify: WARNING — {config['notify']['reply_to_env_var']} is not set, so this digest "
            f"has no Reply-To and no unsubscribe line; replies would go nowhere. See README."
        )

    print(
        f"notify: sending digest ({weekend_count} weekend, {holiday_count} holiday) "
        f"to {len(recipients)} recipient(s), one message each"
    )
    result = send_email(config, subject, text_body, html_body, recipients, reply_to)
    if result["failed"]:
        # Kinds and SMTP codes only — never addresses; this is a public log.
        print(f"notify: {result['failed']} of {len(recipients)} send(s) FAILED — {result['failures']}")

    delivered = result["sent"] > 0
    shown_ids = [
        _flag_id(product, f)
        for product, flags in (("weekend", weekend_flags), ("holiday", holiday_flags))
        for f in (_eligible_fares(flags, min_drop_pct) or [])
    ]
    append_notify_heartbeat(
        _notify_heartbeat_record(
            as_of,
            delivered,
            None if delivered else "all_sends_failed",
            weekend_count,
            holiday_count,
            recipients_count=len(recipients),
            sent_count=result["sent"],
            failed_count=result["failed"],
            # Only what actually went out — an all-failed night records
            # none, so the retry isn't mistaken for a duplicate.
            sent_flag_ids=shown_ids if delivered else None,
        )
    )
    if result["failed"]:
        # After the heartbeat, so the record exists even when this fails the
        # step: the run reddens (the failure email is what's meant to
        # notice) while the recipients who were reachable already have theirs.
        raise RuntimeError(
            f"notify: {result['failed']} of {len(recipients)} sends failed "
            f"({result['failures']}); {result['sent']} delivered."
        )
    return {
        "sent": True,
        "recipients_count": len(recipients),
        "weekend_count": weekend_count,
        "holiday_count": holiday_count,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the text version, write an HTML preview file, send nothing.")
    parser.add_argument(
        "--test",
        metavar="EMAIL",
        help="Send one real email to this address only, ignoring the real recipient list.",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Use this night's flags instead of today's — to preview the format on a day that had flags (--dry-run/--test), or to replay a missed night.",
    )
    parser.add_argument(
        "--resend",
        action="store_true",
        help="Real run only: send even fares already emailed earlier for this date (normally held back so a re-run can't double-email everyone).",
    )
    args = parser.parse_args()

    as_of = date.fromisoformat(args.date) if args.date else None
    result = run(as_of=as_of, dry_run=args.dry_run, test_address=args.test, resend=args.resend)
    print(result)


if __name__ == "__main__":
    main()
