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

Usage:
    python scripts/notify.py                  # send if either product has something new tonight
    python scripts/notify.py --dry-run        # print the text version, write an HTML preview file, send nothing
    python scripts/notify.py --test you@x.com # ONE real email to a single address, ignoring the real recipient list — for reviewing the format before it ever reaches anyone else
"""
import argparse
import html
import json
import os
import smtplib
from collections import defaultdict
from datetime import date, datetime, timezone
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
        "intro": "flights during an upcoming school holiday",
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


def build_digest_text(weekend_flags, holiday_flags, as_of, min_drop_pct):
    """Plain-text digest body combining both products' sections, or
    (None, 0, 0) if neither has anything to say tonight. This is the
    multipart/alternative fallback for clients/screen readers that don't
    render HTML — not a lesser version, a different one, so it's built
    directly rather than stripped-down from the HTML."""
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


def build_digest_html(weekend_flags, holiday_flags, as_of, min_drop_pct):
    """HTML digest body (a complete standalone document) combining both
    products' sections, or (None, 0, 0) if neither has anything to say
    tonight — same eligibility/grouping/sort as the text version, via the
    same _prepare_section() so the two can never disagree about which
    fares qualify."""
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
            This is a nightly signal, not a real-time alert &mdash; the underlying data can be a few days old. If a route above still looks good, worth checking live before booking.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return html_doc, weekend_count, holiday_count


def _load_recipients(config):
    raw = os.environ.get(config["notify"]["recipients_env_var"], "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def send_email(config, subject, text_body, html_body, recipients):
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
    username = config["notify"]["smtp_username"]
    from_address = config["notify"]["from_address"]
    from_name = config["notify"].get("from_name")
    # formataddr, not an f-string, so a name with a space (or anything
    # that needs quoting/escaping) always produces a valid header — the
    # bare address is what still goes to sendmail() below as the SMTP
    # envelope sender, which is a separate thing from this display name.
    from_header = formataddr((from_name, from_address)) if from_name else from_address

    # multipart/alternative, text first then HTML: RFC 2046 has the client
    # render the *last* part it understands, so this prefers HTML where
    # supported and falls back to plain text everywhere else (older
    # clients, screen readers, "always show plain text" settings) rather
    # than sending HTML-only and leaving those with nothing readable.
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_header
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(config["notify"]["smtp_host"], config["notify"]["smtp_port"]) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(from_address, recipients, msg.as_string())


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


def _notify_heartbeat_record(as_of, sent, reason, weekend_count, holiday_count, recipients_count=None):
    record = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "sent": sent,
        "reason": reason,
        "weekend_count": weekend_count,
        "holiday_count": holiday_count,
    }
    if recipients_count is not None:
        record["recipients_count"] = recipients_count
    return record


def run(config=None, as_of=None, dry_run=False, test_address=None):
    config = config or load_config()
    as_of = as_of or datetime.now(timezone.utc).date()
    # --dry-run/--test are manual and exploratory; only the unattended
    # path (what the nightly workflow actually calls) gets a heartbeat
    # entry or a staleness check against it.
    is_real_run = not dry_run and not test_address
    if is_real_run:
        check_notify_staleness(config)

    min_drop_pct = config["detection"]["drop_pct_threshold"]
    weekend_flags = _load_tonight_flags(as_of, "weekend")
    holiday_flags = _load_tonight_flags(as_of, "holiday")

    text_body, weekend_count, holiday_count = build_digest_text(
        weekend_flags, holiday_flags, as_of, min_drop_pct
    )
    if text_body is None:
        print("notify: nothing over the drop threshold tonight — nothing to send")
        if is_real_run:
            append_notify_heartbeat(_notify_heartbeat_record(as_of, False, "no_flags", 0, 0))
        return {"sent": False, "reason": "no_flags", "weekend_count": 0, "holiday_count": 0}

    html_body, _, _ = build_digest_html(weekend_flags, holiday_flags, as_of, min_drop_pct)

    if dry_run:
        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(html_body, encoding="utf-8")
        print("notify: --dry-run, would send (text version):\n")
        print(text_body)
        print(f"\nnotify: HTML version written to {PREVIEW_PATH} for visual review")
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
        print(f"notify: sending ONE test email to {test_address} (not the real recipient list)")
        send_email(config, subject, text_body, html_body, [test_address])
        return {
            "sent": True,
            "recipients": [test_address],
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

    print(
        f"notify: sending digest ({weekend_count} weekend, {holiday_count} holiday) "
        f"to {len(recipients)} recipient(s)"
    )
    send_email(config, subject, text_body, html_body, recipients)
    if is_real_run:
        append_notify_heartbeat(
            _notify_heartbeat_record(as_of, True, None, weekend_count, holiday_count, len(recipients))
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
    args = parser.parse_args()

    result = run(dry_run=args.dry_run, test_address=args.test)
    print(result)


if __name__ == "__main__":
    main()
