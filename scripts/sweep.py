#!/usr/bin/env python3
"""
Flight Deal Scanner — nightly price sweep.

Phase 1, step 1 (see PLAN.md §6): --dry-run only. Hits the confirmed
"everywhere" price endpoint (/v1/prices/cheap — chosen in step 1 after
comparing it live against /aviasales/v3/grouped_prices, see PLAN.md §2) for
a single origin, writes nothing, and dumps the raw response shape to
scratch/ for inspection.

The write path doesn't exist yet — scripts/storage.py lands in step 2.

Usage:
    python scripts/sweep.py --dry-run --origin LHR
"""
import argparse
import json
import os
import time
from datetime import date
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "sweep.yaml"
SCRATCH_DIR = REPO_ROOT / "scratch"


def load_config(path=CONFIG_PATH):
    with open(path, "r") as f:
        return yaml.safe_load(f)


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


def fetch(session, base_url, path, token, token_header, params, limiter, timeout=15):
    """GET one endpoint, header-authed, throttled.

    Returns (status_code_or_None, parsed_json_or_None, raw_text_or_error).
    Never raises on HTTP error status — a 4xx body is exactly the diagnostic
    step 1 wants to see, not something to swallow.
    """
    limiter.wait()
    url = base_url.rstrip("/") + path
    headers = {token_header: token, "Accept-Encoding": "gzip, deflate"}
    try:
        resp = session.get(url, headers=headers, params=params, timeout=timeout)
    except requests.RequestException as e:
        return None, None, f"request failed: {e}"
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body, resp.text


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

    status, body, raw_text = fetch(
        session, base_url, endpoint["path"], token, token_header, params, limiter
    )

    dump_path = SCRATCH_DIR / f"dry_run_{endpoint['name']}_{origin}_{month}.json"
    dump_path.write_text(redact(raw_text or "", token))
    print(f"[{endpoint['name']}] raw response saved: {dump_path}")

    summarize(endpoint["name"], status, body)

    print("\nNothing written to data/.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hit the API, write nothing. Required for now — the write "
        "path (storage.py) doesn't exist yet (PLAN.md §6, step 2).",
    )
    parser.add_argument(
        "--origin",
        help="Single origin to test against. Defaults to the first origin "
        "in config/sweep.yaml. Verify one origin before the full set, "
        "per the brief.",
    )
    parser.add_argument(
        "--month",
        help="YYYY-MM to query. Defaults to next calendar month.",
    )
    args = parser.parse_args()

    config = load_config()

    if not args.dry_run:
        raise SystemExit(
            "Only --dry-run is implemented so far (PLAN.md §6, step 1 of 5). "
            "The write path lands in step 2, once storage.py exists."
        )

    origin = args.origin or config["origins"][0]
    month = args.month or next_month_str(offset=1)

    dry_run(config, origin, month)


if __name__ == "__main__":
    main()
