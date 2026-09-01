#!/usr/bin/env python3
"""
Flight Deal Scanner — weekly digest email (the delivery half of Phase 2).

Reads the last 7 days of data/flags/*.jsonl, builds a plain-text digest,
and emails it via SMTP. No LLM calls anywhere — pure string templating,
matching the brief's "no LLM calls in the sweep path" constraint. Skips
silently on a quiet week: no flags, no email, no digest-day, no email —
the same "empty result is valid" convention as write_delta() and
write_flags().

Runs once a week (config: notify.digest_weekday), checked internally
rather than via a second scheduled workflow — mirrors how the far-months
sweep tier decides for itself whether tonight is its day.

Usage:
    python scripts/notify.py                  # send if today is digest day
    python scripts/notify.py --force          # send regardless of weekday
    python scripts/notify.py --dry-run        # build the digest, print it, send nothing
    python scripts/notify.py --test you@x.com # ONE real email to a single address, ignoring the real recipient list — for reviewing the format before it ever reaches anyone else
"""
import argparse
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import detect
from config import load_config


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


def build_digest(flags, as_of):
    """Plain-text digest body, or None if there's nothing to say. Pure
    templating — no LLM involved, matching the brief's constraint."""
    if not flags:
        return None

    lines = [
        f"Flight Deal Scanner — weekly digest ({as_of.isoformat()})",
        "",
        f"{len(flags)} flight(s) flagged this week:",
        "",
    ]
    for flag in flags:
        lines.append(
            f"  {flag['origin_airport']} -> {flag['destination']}: "
            f"GBP {flag['price_gbp']:.0f} (typically GBP {flag['prior_median_gbp']:.0f}, "
            f"{flag['drop_pct_vs_median'] * 100:.0f}% below)"
        )
        lines.append(
            f"    depart {flag['depart_date']}, return {flag['return_date']}, "
            f"{flag['trip_type']}, flagged {flag['flagged_at']} "
            f"({flag['observation_count']} nights observed before this)"
        )
        lines.append("")

    lines.append(
        "This is a weekly signal, not a real-time alert — the underlying "
        "data can be a few days old. If a route above still looks good, "
        "worth checking live before booking."
    )
    return "\n".join(lines)


def _load_recipients(config):
    raw = os.environ.get(config["notify"]["recipients_env_var"], "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def send_email(config, subject, body, recipients):
    sender = os.environ.get(config["notify"]["sender_env_var"])
    password = os.environ.get(config["notify"]["password_env_var"])
    if not sender or not password:
        raise RuntimeError(
            f"{config['notify']['sender_env_var']} / "
            f"{config['notify']['password_env_var']} not set. Put them in "
            f".env locally (see .env.example) or as GitHub Secrets for the "
            f"workflow. Failing here, at startup, rather than deep inside "
            f"an SMTP call."
        )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(config["notify"]["smtp_host"], config["notify"]["smtp_port"]) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())


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
    body = build_digest(flags, as_of)

    if body is None:
        print("notify: no flags in the last 7 days — quiet week, nothing to send")
        return {"sent": False, "reason": "no_flags", "flags_count": 0}

    if dry_run:
        print("notify: --dry-run, would send:\n")
        print(body)
        return {"sent": False, "reason": "dry_run", "flags_count": len(flags)}

    subject = f"Flight Deal Scanner: {len(flags)} deal(s) this week"

    if test_address:
        print(f"notify: sending ONE test email to {test_address} (not the real recipient list)")
        send_email(config, subject, body, [test_address])
        return {"sent": True, "recipients": [test_address], "flags_count": len(flags), "test": True}

    recipients = _load_recipients(config)
    if not recipients:
        raise RuntimeError(
            f"{config['notify']['recipients_env_var']} is not set or empty "
            f"— nothing to send to."
        )

    print(f"notify: sending digest with {len(flags)} flag(s) to {len(recipients)} recipient(s)")
    send_email(config, subject, body, recipients)
    return {"sent": True, "recipients_count": len(recipients), "flags_count": len(flags)}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--force", action="store_true", help="Send regardless of whether today is the digest day.")
    parser.add_argument("--dry-run", action="store_true", help="Build the digest and print it; send nothing.")
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
