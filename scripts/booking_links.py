"""
Flight Deal Scanner — affiliate booking links.

The digest already names the airline and flight number (scripts/airlines.py,
2026-09-17) — enough to go and search for a fare by hand. This closes the
remaining gap flagged in TODO.md: a direct link. Two steps, kept separate
because only the second one touches the network:

1. build_search_url() — pure string formatting, no request. Encodes
   origin/destination/dates/passengers into the format Travelpayouts'
   "Aviasales affiliate links" doc specifies:
   https://www.aviasales.com/search/{ORIGIN}{DDMM depart}{DEST}{DDMM
   return}{adults}.
2. create_partner_links() — the one network call in this module. A raw
   search URL carries no affiliate credit on its own (per the same doc);
   converting it into a tracked partner_url is a separate, documented API:
   POST /links/v1/create (Travelpayouts' "API for Travelpayouts partner
   links" doc).

Deliberately NOT called from detect.py, even though TODO.md originally
sketched "thread partner_url through detect.py's flag dict" — detect.py's
own docstring rules out API calls ("Pure arithmetic ... no API calls"), so
the network step belongs in sweep.py instead, called after detect.detect()
returns tonight's flags and before they're written. detect.py is unchanged.

trs/marker (config/sweep.yaml's booking_links block) are Travelpayouts
account identifiers, not secrets — marker in particular already appears in
Travelpayouts' own public widget snippets elsewhere on the web. Same "plain
config, not env" treatment as notify.smtp_username/from_address, unlike
TRAVELPAYOUTS_TOKEN.

A conversion failure (bad trs/marker, network error, API downtime) never
fails the nightly sweep over what's an enhancement, not core data
collection: attach_booking_links() catches it, prints a warning, and
leaves every flag with its plain search_url instead of a partner_url.
notify.py prefers partner_url and falls back to search_url, so a
recipient still gets a working link either way — just not an
affiliate-credited one for that one run.
"""
import time
from datetime import date

import requests

_ADULT_DEFAULT = 1
# Hard API limit (Travelpayouts' partner-links doc), not a preference —
# kept as a code constant rather than config for that reason.
_MAX_LINKS_PER_REQUEST = 10
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def build_search_url(origin, destination, depart_date, return_date, adults=_ADULT_DEFAULT):
    """The raw (non-affiliate) Aviasales search-results link. Pure string
    formatting, no network. Format confirmed live against Travelpayouts'
    "Aviasales affiliate links" doc: {ORIGIN}{DDMM depart}{DEST}{DDMM
    return}{adults} — e.g. LGW1110KTW14101 for LGW->KTW, depart 11 Oct,
    return 14 Oct, 1 adult economy. depart_date/return_date accept either
    an ISO string ("YYYY-MM-DD", how a flag stores them) or a date object.
    """
    d = date.fromisoformat(depart_date) if isinstance(depart_date, str) else depart_date
    r = date.fromisoformat(return_date) if isinstance(return_date, str) else return_date
    params = f"{origin}{d.day:02d}{d.month:02d}{destination}{r.day:02d}{r.month:02d}{adults}"
    return f"https://www.aviasales.com/search/{params}"


def _post_with_retry(session, url, token, token_header, payload, max_retries=3, timeout=15):
    """POST once, retrying 429/5xx/network errors with capped exponential
    backoff — same shape as sweep.py's fetch_with_retry(), reimplemented
    small rather than shared, since this is a POST+JSON-body call against
    a different endpoint with a different rate limit, not a GET against
    the price API. Non-retryable statuses (401, 400, ...) raise
    immediately — retrying a bad token or a bad trs/marker wastes calls
    without ever succeeding.
    """
    attempt = 0
    while True:
        try:
            resp = session.post(url, headers={token_header: token}, json=payload, timeout=timeout)
        except requests.RequestException:
            attempt += 1
            if attempt > max_retries:
                raise
            time.sleep(min(2**attempt, 60))
            continue

        if resp.status_code == 200:
            return resp.json()
        attempt += 1
        if resp.status_code not in _RETRYABLE_STATUSES or attempt > max_retries:
            resp.raise_for_status()
        time.sleep(min(2**attempt, 60))


def create_partner_links(urls, token, config):
    """Batch-convert raw search URLs into tracked partner_url values, up to
    _MAX_LINKS_PER_REQUEST at a time, throttled to
    booking_links.requests_per_minute (documented as 100/min *per marker*
    — a different, much higher ceiling than rate_limit.requests_per_minute,
    which throttles the unrelated price-sweep endpoint).

    Returns {url: partner_url} for every URL Travelpayouts accepted. A URL
    missing from the result failed conversion (unsupported brand,
    malformed URL, trs not subscribed, etc.) — the caller falls back to
    the raw URL for it. Raises on a request-level failure (bad token,
    network error, non-2xx after retries); attach_booking_links() is what
    turns that into a soft failure for the whole batch, so this function
    stays honest about what happened rather than silently returning {}.
    """
    if not urls:
        return {}

    booking_cfg = config["booking_links"]
    base_url = config["api"]["base_url"]
    token_header = config["api"]["token_header"]
    url = base_url.rstrip("/") + booking_cfg["create_link_path"]
    min_interval = 60.0 / booking_cfg.get("requests_per_minute", 100)

    session = requests.Session()
    results = {}
    last_call = 0.0
    for i in range(0, len(urls), _MAX_LINKS_PER_REQUEST):
        batch = urls[i : i + _MAX_LINKS_PER_REQUEST]
        elapsed = time.monotonic() - last_call
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        last_call = time.monotonic()

        payload = {
            "trs": booking_cfg["trs"],
            "marker": booking_cfg["marker"],
            "shorten": booking_cfg.get("shorten", True),
            "links": [{"url": u, "sub_id": booking_cfg["sub_id"]} for u in batch],
        }
        body = _post_with_retry(session, url, token, token_header, payload)
        for link in body.get("result", {}).get("links", []):
            if link.get("code") == "success" and link.get("partner_url"):
                results[link["url"]] = link["partner_url"]
    return results


def attach_booking_links(flags, config, token):
    """Enrich each flag dict (in place) with search_url (always, pure) and
    partner_url (when the batched API conversion succeeds). Called from
    sweep.py between detect.detect() and detect.write_flags() — see the
    module docstring for why this isn't inside detect.py itself.

    Never raises: a conversion failure degrades to "every flag keeps its
    plain search_url, no partner_url" rather than reddening the whole
    nightly sweep over what's an enhancement on top of core price data,
    not core data collection (PATTERNS.md's "optional inputs degrade to a
    skip, never a failure").
    """
    if not flags:
        return flags

    for f in flags:
        f["search_url"] = build_search_url(
            f["origin_airport"], f["destination"], f["depart_date"], f["return_date"]
        )

    try:
        partner_urls = create_partner_links([f["search_url"] for f in flags], token, config)
    except Exception as e:
        print(f"  booking_links: partner-link conversion failed, keeping plain search links — {e}")
        return flags

    for f in flags:
        partner_url = partner_urls.get(f["search_url"])
        if partner_url:
            f["partner_url"] = partner_url
    return flags
