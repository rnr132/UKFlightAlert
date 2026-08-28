#!/usr/bin/env python3
"""
Flight Deal Scanner — nightly price sweep.

Two modes:

  --dry-run   Hits the confirmed "everywhere" endpoint (/v1/prices/cheap —
              chosen in step 1 after comparing it live against
              /aviasales/v3/grouped_prices, see PLAN.md §2) for a single
              origin, writes nothing, dumps the raw response to scratch/.

  (default)   The real sweep (step 3, PLAN.md §6): every origin in
              config/sweep.yaml x the near-months tier every night, plus
              the far-months tier on far_sweep_weekday. Throttled,
              retries 429/5xx with backoff (honouring Retry-After when
              present), writes through storage.ingest(), and appends one
              line to data/heartbeat.jsonl per run.

Usage:
    python scripts/sweep.py --dry-run --origin LHR   # inspect a response
    python scripts/sweep.py --origin LHR             # real sweep, one origin
    python scripts/sweep.py                          # real sweep, all origins
"""
import argparse
import json
import os
import time
from datetime import date, datetime, timezone

import requests

import storage
from config import REPO_ROOT, load_config

SCRATCH_DIR = REPO_ROOT / "scratch"
HEARTBEAT_PATH = REPO_ROOT / "data" / "heartbeat.jsonl"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def load_token(config):
    """Read the token from the environment. Never accept it as a CLI arg —
    that would put it in process listings and in `set -x` logs
    (PATTERNS.md §2's pattern, carried over deliberately).
    """
    var = config["api"]["token_env_var"]
    token = os.environ.get(var)
    if not token:
        raise RuntimeError(
            f"{var} is not set. Put it in .env (see .env.example) and "
            f"export it before running. Failing here, at startup, rather "
            f"than deep inside an HTTP call."
        )
    return token


def redact(text, token):
    """Strip a token value out of any string before it's written or printed.

    Belt and braces: auth goes via header so the token should never reach a
    URL or error body, but PATTERNS.md §2 flags a real project that dumped
    2000 chars of raw output into a public log on parse failure. The same
    trap here is worse, since the token is also a valid *request parameter*
    on these endpoints (just not one we use) — redact regardless of path.
    """
    if not text or not token:
        return text
    return text.replace(token, "<redacted>")


def next_month_str(offset=1):
    """YYYY-MM for `offset` months from today."""
    d = date.today()
    month_index = d.month - 1 + offset
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return f"{year:04d}-{month:02d}"


class RateLimiter:
    """Sleep between calls so throughput stays under N/minute.

    Step 1 only ever makes a couple of calls, but every request path —
    dry-run included — should go through the same throttle from day one.
    Full retry-with-backoff on 429/Retry-After lands in step 3 (PLAN.md §6)
    once the real sweep loop exists.
    """

    def __init__(self, requests_per_minute):
        self.min_interval = 60.0 / requests_per_minute
        self._last_call = 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last_call
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


class FetchResult:
    """Small structured result instead of a growing bare tuple — step 3
    needs the Retry-After header and a distinguishable network-error case
    on top of what step 1's dry run needed, so this replaces the old
    (status, body, raw_text) tuple return.
    """

    def __init__(self, status=None, body=None, raw_text="", retry_after=None, error=None):
        self.status = status
        self.body = body
        self.raw_text = raw_text
        self.retry_after = retry_after
        self.error = error

    @property
    def ok(self):
        return self.error is None and self.status == 200


def fetch(session, base_url, path, token, token_header, params, limiter, timeout=15):
    """GET one endpoint, header-authed, throttled. Never raises on HTTP
    error status — a 4xx/5xx body is exactly the diagnostic worth seeing,
    not something to swallow.
    """
    limiter.wait()
    url = base_url.rstrip("/") + path
    headers = {token_header: token, "Accept-Encoding": "gzip, deflate"}
    try:
        resp = session.get(url, headers=headers, params=params, timeout=timeout)
    except requests.RequestException as e:
        return FetchResult(error=str(e))

    try:
        body = resp.json()
    except ValueError:
        body = None

    retry_after = None
    if resp.status_code == 429:
        raw_retry_after = resp.headers.get("Retry-After")
        if raw_retry_after:
            try:
                retry_after = float(raw_retry_after)
            except ValueError:
                retry_after = None

    return FetchResult(status=resp.status_code, body=body, raw_text=resp.text, retry_after=retry_after)


def fetch_with_retry(session, base_url, path, token, token_header, params, limiter, max_retries, timeout=15):
    """fetch(), retrying 429/5xx/network errors with backoff.

    Retry-After is honoured when the API sends one (capped at 120s so a
    stray or hostile header can't stall the whole run); otherwise backs
    off exponentially, capped at 60s. Non-retryable statuses (401, 400,
    404, ...) return immediately on the first attempt — retrying a bad
    token or a bad param wastes calls without ever succeeding.
    """
    attempt = 0
    while True:
        result = fetch(session, base_url, path, token, token_header, params, limiter, timeout)
        if result.ok:
            return result

        attempt += 1
        retryable = result.error is not None or result.status in RETRYABLE_STATUSES
        if not retryable or attempt > max_retries:
            return result

        delay = min(result.retry_after, 120) if result.retry_after else min(2 ** attempt, 60)
        print(
            f"    retrying in {delay:.0f}s (attempt {attempt}/{max_retries}) — "
            f"status={result.status} error={result.error}"
        )
        time.sleep(delay)


def summarize(name, status_code, body):
    """Print a shape summary to stdout — never the raw body (PLAN.md §5)."""
    print(f"\n--- {name} ---")
    print(f"status: {status_code}")
    if body is None:
        print("body: not valid JSON — see the scratch/ dump for raw text")
        return
    if isinstance(body, dict):
        print(f"top-level keys: {sorted(body.keys())}")
        data = body.get("data", body.get("result"))
        if isinstance(data, list):
            print(f"data: list, {len(data)} rows")
            if data:
                print(f"sample row keys: {sorted(data[0].keys())}")
                print(f"sample row:\n{json.dumps(data[0], indent=2)[:600]}")
        elif isinstance(data, dict):
            print(f"data: dict, keys: {sorted(data.keys())}")
        elif data is not None:
            print(f"data: {type(data).__name__} = {str(data)[:200]}")
        else:
            print("no 'data' or 'result' key — full top-level body:")
            print(json.dumps(body, indent=2)[:600])
    elif isinstance(body, list):
        print(f"body: list, {len(body)} rows")
        if body:
            print(f"sample row keys: {sorted(body[0].keys())}")
            print(f"sample row:\n{json.dumps(body[0], indent=2)[:600]}")
    else:
        print(f"body: {type(body).__name__} = {str(body)[:200]}")


def dry_run(config, origin, month):
    token = load_token(config)
    limiter = RateLimiter(config["rate_limit"]["requests_per_minute"])
    session = requests.Session()
    base_url = config["api"]["base_url"]
    token_header = config["api"]["token_header"]
    currency = config["market"]["currency"].lower()

    SCRATCH_DIR.mkdir(exist_ok=True)

    endpoint = config["api"]["endpoint"]
    params = dict(endpoint.get("params") or {})
    params.setdefault("origin", origin)
    params.setdefault("currency", currency)
    month_param = endpoint.get("month_param")
    if month_param:
        params.setdefault(month_param, month)

    print(f"DRY RUN — origin={origin} month={month} currency={currency}")
    print("Writes nothing. Hitting the real API. Raw response saved under scratch/.\n")

    result = fetch(session, base_url, endpoint["path"], token, token_header, params, limiter)

    dump_path = SCRATCH_DIR / f"dry_run_{endpoint['name']}_{origin}_{month}.json"
    dump_path.write_text(redact(result.raw_text or result.error or "", token))
    print(f"[{endpoint['name']}] raw response saved: {dump_path}")

    summarize(endpoint["name"], result.status, result.body)

    print("\nNothing written to data/.")


def append_heartbeat(record):
    """One JSON line per run, appended — not overwritten — so the file
    doubles as a history (PLAN.md §5's fix for silent gaps) and as the
    staleness check's own memory of when a run last happened at all.
    """
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_PATH, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def check_staleness(config, as_of=None):
    """Warn loudly, at startup, if the last recorded run is older than
    expected — PATTERNS.md §4.4's fix, applied from day one instead of
    left for later. Reads the heartbeat log rather than the data itself,
    since a genuinely quiet night (nothing changed) still appends a
    heartbeat — deriving staleness from data would misread "sweep ran,
    found nothing new" as "sweep didn't run".
    """
    as_of = as_of or datetime.now(timezone.utc)
    threshold = config.get("monitoring", {}).get("staleness_warning_hours", 36)

    if not HEARTBEAT_PATH.exists():
        print("No heartbeat log yet — this looks like the first run.")
        return

    with open(HEARTBEAT_PATH) as f:
        lines = [line for line in f if line.strip()]
    if not lines:
        print("Heartbeat log exists but is empty — treating as first run.")
        return

    last = json.loads(lines[-1])
    last_run_at = datetime.fromisoformat(last["run_started_at"])
    gap_hours = (as_of - last_run_at).total_seconds() / 3600.0

    if gap_hours > threshold:
        print(
            f"*** WARNING: last sweep was {gap_hours:.1f}h ago, over the "
            f"{threshold}h threshold. A scheduled run may have been "
            f"skipped or failed — check the Actions tab. ***"
        )
    else:
        print(f"Last sweep was {gap_hours:.1f}h ago (within the {threshold}h threshold).")


def sweep_one(session, config, token, token_header, base_url, limiter, origin, month, observed_at, sweep_date, max_retries):
    """Fetch one (origin, month) pair — covering every destination in one
    call, per the endpoint choice in PLAN.md §2 — and ingest it.
    """
    endpoint = config["api"]["endpoint"]
    currency = config["market"]["currency"].lower()
    params = dict(endpoint.get("params") or {})
    params.setdefault("origin", origin)
    params.setdefault("currency", currency)
    month_param = endpoint.get("month_param")
    if month_param:
        params.setdefault(month_param, month)

    result = fetch_with_retry(
        session, base_url, endpoint["path"], token, token_header, params, limiter, max_retries
    )

    if not result.ok:
        detail = f"status={result.status} error={redact(result.error or '', token)}"
        print(f"  [{origin} {month}] FAILED — {detail}")
        return {"origin": origin, "month": month, "ok": False, "fetched": 0, "changed": 0, "cheapest": None, "detail": detail}

    try:
        ingest_result = storage.ingest(result.body, origin, observed_at=observed_at, sweep_date=sweep_date)
    except ValueError as e:
        # Shape didn't match what normalize_response expects — surface it,
        # per the brief's explicit instruction, rather than skip silently.
        print(f"  [{origin} {month}] FAILED to parse response — {e}")
        return {"origin": origin, "month": month, "ok": False, "fetched": 0, "changed": 0, "cheapest": None, "detail": str(e)}

    print(f"  [{origin} {month}] fetched={ingest_result['fetched']} changed={ingest_result['changed']}")
    return {"origin": origin, "month": month, "ok": True, **ingest_result}


def real_sweep(config, origin_filter=None):
    check_staleness(config)

    token = load_token(config)
    limiter = RateLimiter(config["rate_limit"]["requests_per_minute"])
    max_retries = config["rate_limit"].get("max_retries", 3)
    session = requests.Session()
    base_url = config["api"]["base_url"]
    token_header = config["api"]["token_header"]

    run_started_at = datetime.now(timezone.utc)
    sweep_date = run_started_at.date()

    near_months = config["horizon"]["near_months"]
    far_months = config["horizon"]["far_months"]
    far_weekday = config["horizon"].get("far_sweep_weekday", 6)
    is_far_night = sweep_date.weekday() == far_weekday

    months = [next_month_str(offset=i) for i in range(near_months)]
    if is_far_night:
        months += [next_month_str(offset=near_months + i) for i in range(far_months)]

    origins = [origin_filter] if origin_filter else config["origins"]

    print(
        f"SWEEP — {len(origins)} origin(s) x {len(months)} months "
        f"({'near+far' if is_far_night else 'near only'}) = {len(origins) * len(months)} calls\n"
    )

    results = []
    for origin in origins:
        for month in months:
            results.append(
                sweep_one(session, config, token, token_header, base_url, limiter, origin, month, run_started_at, sweep_date, max_retries)
            )

    # Maintenance runs every night, unconditionally — both are no-ops on
    # fresh data (nothing old enough to fold or roll up yet) and cost
    # nothing to call. Doing it here means one workflow step (`python
    # sweep.py`) is the whole pipeline; without this, deltas would
    # accumulate forever and the git-history-size problem the delta
    # design exists to solve would come back by omission.
    compact_result = storage.compact_deltas()
    rollup_result = storage.rollup_stale(raw_days=config["retention"]["raw_days"])
    print(f"\ncompact_deltas: {compact_result}")
    print(f"rollup_stale: {rollup_result}")

    failures = [r for r in results if not r["ok"]]
    cheapest = min(
        (r["cheapest"] for r in results if r.get("cheapest")),
        key=lambda c: c["price_gbp"],
        default=None,
    )
    heartbeat = {
        "run_started_at": run_started_at.isoformat(),
        "sweep_date": sweep_date.isoformat(),
        "is_far_sweep_night": is_far_night,
        "origins": origins,
        "months": months,
        "calls_attempted": len(results),
        "calls_failed": len(failures),
        "rows_fetched": sum(r["fetched"] for r in results),
        "rows_changed": sum(r["changed"] for r in results),
        "cheapest": cheapest,
        "compact": compact_result,
        "rollup": rollup_result,
    }
    append_heartbeat(heartbeat)

    print(f"\nHEARTBEAT: {json.dumps(heartbeat, default=str)}")

    if failures:
        print(f"\n{len(failures)} of {len(results)} origin-month calls failed:")
        for r in failures:
            print(f"  {r['origin']} {r['month']}: {r['detail']}")
        # Exit non-zero so the Action shows red — a partial failure should
        # look broken, not silently green (PATTERNS.md §4.4) — but
        # everything successfully fetched above, plus this run's
        # compaction/rollup, has already been written. The workflow commits
        # it regardless of this exit code (PLAN.md §6, step 4): partial
        # data beats no data, and the failure should still be visible.
        raise SystemExit(1)

    print("\nSweep complete, no failures.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hit the API, write nothing, and dump the raw response for "
        "inspection instead of running the real sweep.",
    )
    parser.add_argument(
        "--origin",
        help="Restrict to a single origin — works for both --dry-run and "
        "the real sweep. Verify one origin before the full set, per the brief.",
    )
    parser.add_argument(
        "--month",
        help="--dry-run only: YYYY-MM to query. Defaults to next calendar month.",
    )
    args = parser.parse_args()

    config = load_config()

    if args.dry_run:
        origin = args.origin or config["origins"][0]
        month = args.month or next_month_str(offset=1)
        dry_run(config, origin, month)
    else:
        real_sweep(config, origin_filter=args.origin)


if __name__ == "__main__":
    main()
