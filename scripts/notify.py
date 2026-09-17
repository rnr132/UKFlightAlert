#!/usr/bin/env python3
"""
Flight Deal Scanner — weekly digest email (the delivery half of Phase 2).

Reads the last 7 days of data/flags/*.jsonl and emails a digest — as of
2026-09-17, a styled HTML version (table-based layout, inline styles, no
external images or fonts — nothing for an email client to block or fail
to load) with a plain-text alternative in the same message, not HTML
alone. No LLM calls anywhere, in either version — pure string/template
building, matching the brief's "no LLM calls in the sweep path"
constraint. Skips silently on a quiet week: no flags, no email, no
digest-day, no email — the same "empty result is valid" convention as
write_delta() and write_flags().

Runs once a week (config: notify.digest_weekday), checked internally
rather than via a second scheduled workflow — mirrors how the far-months
sweep tier decides for itself whether tonight is its day.

Usage:
    python scripts/notify.py                  # send if today is digest day
    python scripts/notify.py --force          # send regardless of weekday
    python scripts/notify.py --dry-run        # print the text version, write an HTML preview file, send nothing
    python scripts/notify.py --test you@x.com # ONE real email to a single address, ignoring the real recipient list — for reviewing the format before it ever reaches anyone else
"""
import argparse
import html
import json
import os
import smtplib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import detect
import places
import airlines
import school_holidays
from config import REPO_ROOT, load_config

PREVIEW_PATH = REPO_ROOT / "scratch" / "digest_preview.html"


def _load_recent_flags(as_of, days=7):
    """All flags from the last `days` calendar days, oldest first."""
    flags = []
    for i in range(days):
        day = as_of - timedelta(days=i)
        path = detect.FLAGS_DIR / f"{day.isoformat()}.jsonl"
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    flags.append(json.loads(line))
    flags.sort(key=lambda flag: flag["flagged_at"])
    return flags


def _trip_nights(flag):
    return (date.fromisoformat(flag["return_date"]) - date.fromisoformat(flag["depart_date"])).days


def _fmt_date(iso_str):
    """2026-10-01 -> 1-Oct-26. Built by hand rather than strftime('%-d')
    since the no-leading-zero directive isn't portable across platforms."""
    d = date.fromisoformat(iso_str)
    return f"{d.day}-{d.strftime('%b')}-{d.strftime('%y')}"


def _prepare_digest(flags, min_drop_pct, min_trip_nights=0):
    """Filter to fares that dropped at least `min_drop_pct` and stay at
    least `min_trip_nights` nights, collapse repeat flags of the same
    itinerary to the latest (which by the re-flag rule in detect.py is
    also the lowest), then group region -> destination -> [fares].

    Returns (tree, count) where count is the number of *unique itineraries
    actually shown* — not len(flags). Those differ in two real ways: a
    dropping fare can be flagged more than once in the 7-day window (only
    the latest is shown), and raising min_drop_pct after some flags were
    already written at a lower bar means old flags sitting in the JSONL
    files no longer clear it. `count` is what belongs in a subject line or
    "N fares" summary; the raw flags list is not — using it there was a
    pre-existing quirk (harmless while the threshold was stable, wrong the
    moment it changes), fixed here rather than carried forward.

    Returns (None, 0) if nothing clears the bar — the shared "nothing to
    send" signal both renderers and run() check.
    """
    eligible = [
        f
        for f in flags
        if f["drop_pct_vs_median"] >= min_drop_pct and _trip_nights(f) >= min_trip_nights
    ]
    if not eligible:
        return None, 0

    latest = {}
    for f in eligible:
        k = (f["origin_airport"], f["destination"], f["depart_date"], f["return_date"])
        if k not in latest or f["flagged_at"] > latest[k]["flagged_at"]:
            latest[k] = f

    tree = defaultdict(lambda: defaultdict(list))
    for f in latest.values():
        city, country, continent = places.resolve(f["destination"])
        tree[continent][f["destination"]].append(
            {
                **f,
                "_city": city,
                "_country": country,
                "_holiday": school_holidays.nearby(f),
            }
        )
    return tree, len(latest)


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
    first)."""
    label, relation = holiday
    return f"{_HOLIDAY_RELATION_PHRASING[relation]} {label}"


def _airline_label(f):
    """'Vueling VY1234' — the name someone would actually search for to
    book this exact flight, not just the 2-letter code. Falls back to the
    raw code if airlines.resolve() doesn't know it (never crashes, never
    hides the flight number even when the name is unknown).

    None when the flag predates this field (added 2026-09-17 — the 7-day
    window mixes old and new flags for the next week, and there's nothing
    to backfill an already-written record with), so callers can treat
    "no airline info" the same way they already treat "no holiday match"
    — check truthiness, render nothing, never crash on an old record."""
    airline = f.get("airline")
    flight_number = f.get("flight_number")
    if not airline or not flight_number:
        return None
    return f"{airlines.resolve(airline)} {airline}{flight_number}"


def build_digest_text(flags, as_of, min_drop_pct, min_trip_nights=0):
    """Plain-text digest body, or None if there's nothing to say. This is
    the multipart/alternative fallback for clients/screen readers that
    don't render HTML — not a lesser version, a different one, so it's
    built directly rather than stripped-down from the HTML."""
    tree, count = _prepare_digest(flags, min_drop_pct, min_trip_nights)
    if tree is None:
        return None

    pct_bar = round(min_drop_pct * 100)
    fare_word = "fare" if count == 1 else "fares"
    lines = [
        f"London Flight Deals — weekly digest ({_fmt_date(as_of.isoformat())})",
        "",
        f"{count} {fare_word} at least {pct_bar}% below their recent typical price,",
        "grouped by region then destination, biggest drop first.",
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
                    f"    {f['origin_airport']} -> {dcode}  "
                    f"GBP {f['price_gbp']:.0f}  "
                    f"(typically GBP {f['prior_median_gbp']:.0f}, {pct}% below)"
                )
                airline_label = _airline_label(f)
                if airline_label:
                    lines.append(f"      {airline_label}")
                lines.append(
                    f"      {_fmt_date(f['depart_date'])} to {_fmt_date(f['return_date'])}  "
                    f"({nights} night{'s' if nights != 1 else ''})  "
                    f"flagged {_fmt_date(f['flagged_at'])}"
                )
                if f["_holiday"]:
                    lines.append(f"      ★ {_holiday_phrase(f['_holiday'])}")
            lines.append("")
        lines.append("")

    lines.append(
        "This is a weekly signal, not a real-time alert — the underlying "
        "data can be a few days old. If a route above still looks good, "
        "worth checking live before booking."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering (2026-09-17). Table-based layout with inline styles on
# every structurally-important element — not because <style> blocks never
# work in email, but because inline is the one thing that renders the same
# everywhere from Gmail to Outlook to a phone's mail app, and this only
# gets built and eyeballed occasionally, not iterated on live against a
# test suite of real inboxes. A light <style> block still carries the
# few things safe to leave there (link colour, box-sizing). No remote
# images or web fonts: nothing for a client's image-blocking to break,
# nothing to fail to load — a system font stack renders natively on every
# platform anyway.
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


def _render_fare_row(f, dcode, is_last):
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
              {_esc(f['origin_airport'])} &rarr; {_esc(dcode)} &middot; {_fmt_date(f['depart_date'])} to {_fmt_date(f['return_date'])} &middot; {nights} night{'s' if nights != 1 else ''}
            </td>
          </tr>{_render_airline_row(f)}{_render_holiday_tag(f['_holiday'])}
        </table>
      </td></tr>"""


def _render_airline_row(f):
    """A muted line under the route/dates line naming the airline and
    flight number — the detail someone actually needs to go and book this
    (a route/price alone isn't bookable; "Vueling VY1234" is). Empty
    string when the flag predates this field (_airline_label() returns
    None for those), same "splice in unconditionally" convention as
    _render_holiday_tag()."""
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
    it reads as a different *kind* of signal (timing, not price) rather
    than another price tier. Text carries the during/before/after
    distinction (_holiday_phrase()), not the colour — one consistent tag
    style, since that's what was actually asked for. Empty string when
    there's no match, so the caller can always splice this in
    unconditionally."""
    if not holiday:
        return ""
    return f"""
          <tr>
            <td colspan="2" style="padding-top:6px;">
              <span style="display:inline-block;background:#f3e8ff;color:#6b21a8;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;font-family:{_FONT_STACK};">&#127890; {_esc(_holiday_phrase(holiday))}</span>
            </td>
          </tr>"""


def _render_destination_card(dcode, fares):
    head = fares[0]
    place = _place_label(head, dcode)
    rows = "".join(
        _render_fare_row(f, dcode, is_last=(i == len(fares) - 1)) for i, f in enumerate(fares)
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


def build_digest_html(flags, as_of, min_drop_pct, min_trip_nights=0):
    """HTML digest body (a complete standalone document), or None if
    there's nothing to say — same eligibility/grouping/sort as the text
    version, via the same _prepare_digest() so the two can never disagree
    about which fares qualify."""
    tree, count = _prepare_digest(flags, min_drop_pct, min_trip_nights)
    if tree is None:
        return None

    pct_bar = round(min_drop_pct * 100)
    fare_word = "fare" if count == 1 else "fares"
    date_label = _fmt_date(as_of.isoformat())
    sections = "".join(_render_continent_section(c, tree) for c in _sorted_continents(tree))

    return f"""<!doctype html>
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
    {count} {fare_word} at least {pct_bar}% below their usual price this week &mdash; {_esc(', '.join(_sorted_continents(tree)))}.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;">
        <tr><td style="background:#0f172a;padding:28px 24px;border-radius:12px 12px 0 0;">
          <div style="color:#ffffff;font-size:20px;font-weight:700;font-family:{_FONT_STACK};">&#9992;&#65039; London Flight Deals</div>
          <div style="color:#94a3b8;font-size:13px;padding-top:6px;font-family:{_FONT_STACK};">Weekly digest &middot; {date_label}</div>
        </td></tr>
        <tr><td style="background:#ffffff;padding:20px 24px 4px;">
          <p style="margin:0;font-size:15px;color:#334155;line-height:1.5;font-family:{_FONT_STACK};">
            <strong>{count} {fare_word}</strong> at least <strong>{pct_bar}%</strong> below their recent typical price, grouped by region then destination, biggest drop first.
          </p>
        </td></tr>
        <tr><td style="background:#ffffff;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{sections}
          </table>
        </td></tr>
        <tr><td style="background:#ffffff;padding:8px 24px 28px;border-radius:0 0 12px 12px;">
          <p style="margin:20px 0 0;font-size:12px;color:#94a3b8;line-height:1.6;border-top:1px solid #e2e8f0;padding-top:16px;font-family:{_FONT_STACK};">
            This is a weekly signal, not a real-time alert &mdash; the underlying data can be a few days old. If a route above still looks good, worth checking live before booking.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _load_recipients(config):
    raw = os.environ.get(config["notify"]["recipients_env_var"], "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def send_email(config, subject, text_body, html_body, recipients):
    # Resend's SMTP model splits "login identity" from "sender identity"
    # (2026-09-17 — see PLAN.md's delivery section for why Gmail/Yahoo were
    # dropped): smtp_username is a fixed literal, not a secret, so it lives
    # in config/sweep.yaml like smtp_host/smtp_port; from_address is the
    # visible sender on every email regardless, so it's plain config too.
    # Only the API key is a secret.
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


def run(config=None, as_of=None, force=False, dry_run=False, test_address=None):
    config = config or load_config()
    as_of = as_of or datetime.now(timezone.utc).date()

    is_digest_day = as_of.weekday() == config["notify"]["digest_weekday"]
    if not (force or is_digest_day or test_address):
        print(
            f"notify: today ({as_of}, weekday={as_of.weekday()}) isn't the "
            f"digest day ({config['notify']['digest_weekday']}) — skipping"
        )
        return {"sent": False, "reason": "not_digest_day"}

    flags = _load_recent_flags(as_of)
    min_drop_pct = config["detection"]["drop_pct_threshold"]
    min_trip_nights = config["detection"].get("min_trip_nights", 0)

    text_body = build_digest_text(flags, as_of, min_drop_pct, min_trip_nights)
    if text_body is None:
        print("notify: nothing over the drop threshold in the last 7 days — nothing to send")
        return {"sent": False, "reason": "no_flags", "flags_count": 0}
    html_body = build_digest_html(flags, as_of, min_drop_pct, min_trip_nights)
    _, count = _prepare_digest(flags, min_drop_pct, min_trip_nights)

    if dry_run:
        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(html_body, encoding="utf-8")
        print("notify: --dry-run, would send (text version):\n")
        print(text_body)
        print(f"\nnotify: HTML version written to {PREVIEW_PATH} for visual review")
        return {"sent": False, "reason": "dry_run", "flags_count": count}

    subject = f"London Flight Deals: {count} deal(s) this week"

    if test_address:
        print(f"notify: sending ONE test email to {test_address} (not the real recipient list)")
        send_email(config, subject, text_body, html_body, [test_address])
        return {"sent": True, "recipients": [test_address], "flags_count": count, "test": True}

    recipients = _load_recipients(config)
    if not recipients:
        raise RuntimeError(
            f"{config['notify']['recipients_env_var']} is not set or empty "
            f"— nothing to send to."
        )

    print(f"notify: sending digest with {count} flag(s) to {len(recipients)} recipient(s)")
    send_email(config, subject, text_body, html_body, recipients)
    return {"sent": True, "recipients_count": len(recipients), "flags_count": count}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--force", action="store_true", help="Send regardless of whether today is the digest day.")
    parser.add_argument("--dry-run", action="store_true", help="Print the text version, write an HTML preview file, send nothing.")
    parser.add_argument(
        "--test",
        metavar="EMAIL",
        help="Send one real email to this address only, ignoring the real recipient list.",
    )
    args = parser.parse_args()

    result = run(force=args.force, dry_run=args.dry_run, test_address=args.test)
    print(result)


if __name__ == "__main__":
    main()
