# TODO

Operational reminders that don't belong in `PLAN.md` (Phase 1's architecture
record, now closed) or `README.md` (how the system runs) — ongoing things a
human needs to actually go *do*, independent of any single phase.

---

## Book at least one real trip through Travelpayouts/Aviasales

**Why this matters:** Travelpayouts gives Data API access for free because
they expect it to drive bookings — that's the whole business model (see
`Brief.md`: "affiliate-monetised rather than per-call"). This project has
been pulling ~70 calls a night since 2026-08-28 and will keep doing so for
at least the 18-month horizon in `Brief.md`, sending zero bookings back the
whole time. An account that only ever takes and never converts is a
plausible candidate for throttling or revocation — and losing the token
mid-way through the four-week (or longer) accumulation window would be a
much worse day than a booked flight.

**What to do:** the next time anyone in the family actually books a flight,
route it through a Travelpayouts/Aviasales affiliate link on this account
rather than booking direct or through another site. It doesn't need to be
related to this project's swept routes — any real, completed booking is
plausibly what keeps the account looking legitimate to them.

**How:** Travelpayouts' affiliate/deep-link tools are at
[travelpayouts.com/programs/100/tools](https://www.travelpayouts.com/programs/100/tools) —
check there for the current recommended way to generate a trackable link.
Worth knowing, though it's not written down anywhere durable: the
`/aviasales/v3/grouped_prices` response inspected during step 1's dry run
had a `link` field carrying what looked like an Aviasales deep link with
tracking parameters attached. That scratch file is gone now (it lived in
the gitignored `scratch/`, since cleaned up), `grouped_prices` isn't wired
into `sweep.py` at all any more (dropped after step 1 in favour of
`prices_cheap` — see `PLAN.md §2`), and this fact only survives in this
session's conversation history. To look at it again later, hit the
endpoint directly rather than expecting the current scripts to do it —
e.g. a one-off `requests.get(...)` call against
`https://api.travelpayouts.com/aviasales/v3/grouped_prices` with the
`X-Access-Token` header, the way step 1's investigation itself did it
before `sweep.py`'s dry-run mode existed.

**Cadence:** no hard deadline was set for this — it's a "don't let it go to
zero for too long" reminder, not a scheduled task. A sensible-sounding
target would be at least once within Phase 1's data-collection window, but
that's a suggestion, not something from the brief.

---

## Waiting on Yahoo account aging before the live test send (2026-09-XX)

Delivery mechanism is decided (email, weekly digest) and `scripts/notify.py`
is fully built and tested — see `PLAN.md`'s delivery section. Telegram was
ruled out; Gmail, Proton, Outlook.com, and Zoho were all considered as the
sender and each hit a real wall (account-creation rate limits after
creating several new accounts in one week; Proton free tier can't send
without a paid-plan Bridge setup or a Business-tier SMTP token; Outlook.com
is retiring basic-auth app passwords for real OAuth2; Zoho's clean free
tier is tied to owning a custom domain). Yahoo Mail is the one with no
structural blocker — but a **new** Yahoo account can't generate an app
password until it looks like an established one to Yahoo's own fraud
heuristics, no published timeframe, anecdotally days to weeks.

**What's actually pending:** the Yahoo account needs to age into
eligibility. Worth *using* it normally in the meantime (logging in,
sending/receiving a few real emails) rather than leaving it dormant, since
"consistent normal usage" is the literal criterion.

**Nothing else is blocked by this.** The nightly sweep, detection, and
retention pipeline all run independently of delivery being resolved.

**Once the app password works:**
1. `python scripts/notify.py --test <address>` — one real email, format
   review, before anyone else ever sees one.
2. Update `config/sweep.yaml`'s `notify.smtp_host`/`smtp_port` to Yahoo's
   (`smtp.mail.yahoo.com`, 587) — currently still set to Gmail's values.
3. Add `SMTP_USER`/`SMTP_PASSWORD`/`NOTIFY_RECIPIENTS` as GitHub Secrets
   (the real family/friend list this time, not the test address).
4. Wire `notify.py` into `sweep.py` and the workflow, the same way
   `detect.py` already is — as its own explicit step, not bundled
   silently into the test.

---

## Re-pin GitHub Actions to newer SHAs — now automated

**Done once by hand on 2026-09-10:** `actions/checkout` v4.4.0 → v7.0.1,
`actions/setup-python` v5.6.0 → v7.0.0, in both workflow files. Both new
majors (checkout v5, setup-python v6) target Node 24 natively, so the
"Node.js 20 is deprecated … forced to run on Node.js 24" notice that had
printed on every run since 2026-08-28 is gone. Release notes for the
major jumps were checked first — the only breaking changes
(`pull_request_target` fork-checkout defaults, persist-credentials
location, Node 24 minimum runner) don't touch a `schedule` +
`workflow_dispatch`, checkout-then-commit workflow on GitHub-hosted
runners.

**Standing check, so this doesn't need remembering again:**
`scripts/check_action_pins.py`, run quarterly by
`.github/workflows/check-action-pins.yml` (09:27 UTC, 1st of
Jan/Apr/Jul/Oct) and on demand via `workflow_dispatch`. It reads every
SHA-pinned `uses:` out of the workflow files, asks the GitHub API for
each action's latest release, and opens (or refreshes) a single
`action-pins`-labelled issue — *Re-pin GitHub Actions to newer SHAs* —
carrying the exact old → new SHA and `# vX.Y.Z` comment to write.
Everything-current is a clean no-op run, no issue. Change `*/3` to `*/4`
in the cron for a four-monthly cadence instead of quarterly.

When the issue shows up: re-resolve each SHA independently first (the
issue body includes the `git ls-remote --tags` line), skim that
release's notes for anything that touches this kind of workflow, bump
the pins, close the issue.
