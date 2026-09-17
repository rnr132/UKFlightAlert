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

## Resend domain verification — the remaining delivery setup step (2026-09-17)

**Decided:** Resend (free transactional-email tier), replacing the
Gmail/Yahoo consumer-webmail search. Full reasoning in `PLAN.md`'s
delivery section — short version: Gmail rate-limited *creating* a new
account after several attempts in one week, and Yahoo gated *app-password
issuance* behind a "does this look like an established account" fraud
heuristic that stayed unresolved for two-plus weeks with no published
timeframe. Both are anti-abuse gates aimed at a human signing up for a
mailbox, which this never was. Resend is built for programmatic sending
and has no such gate — it asks for a one-time domain verification instead,
which is slower to describe but has a definite end.

`config/sweep.yaml`, `.env.example`, and `scripts/notify.py` are already
updated for Resend's SMTP relay (`smtp.resend.com`, fixed username
`resend`, an API key as the password). What's left needs a human — account
creation and DNS changes aren't things to automate:

1. Create a free Resend account at [resend.com](https://resend.com) — no
   card required.
2. **Add and verify a sending domain.** Recommend a dedicated subdomain of
   `rohit-nair.com` rather than the bare domain — e.g.
   `flightalert.rohit-nair.com` — so this stays isolated from the main
   domain's mail reputation and from whatever the personal site's own
   email eventually does. Resend's dashboard generates the exact DNS
   records (DKIM/SPF, a couple of TXT/CNAME entries) to add at the
   domain's DNS host once the subdomain is entered.
3. Update `config/sweep.yaml`'s `notify.from_address` to match whatever
   local part is chosen on the verified domain
   (`deals@flightalert.rohit-nair.com` is the current placeholder).
4. Generate an API key at
   [resend.com/api-keys](https://resend.com/api-keys) and put it in
   `.env` locally as `SMTP_PASSWORD`.
5. `python scripts/notify.py --test <address>` — one real email, format
   review, before anyone else ever sees one.
6. Add `SMTP_PASSWORD`/`NOTIFY_RECIPIENTS` as GitHub Secrets (the real
   family/friend list this time, not the test address) — `smtp_username`
   and `from_address` are plain config now, not secrets, so neither needs
   one.
7. Wire `notify.py` into `sweep.py` and the workflow, the same way
   `detect.py` already is — as its own explicit step, not bundled
   silently into the test.

**Nothing else is blocked by this.** The nightly sweep, detection, and
retention pipeline all run independently of delivery being resolved — true
before this pivot and still true after.

**The Yahoo account** needs nothing further — just stop the deliberate
"use it normally so it ages" routine, since it's no longer on the critical
path.

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

---

## Future: a real booking link on each digest entry

Raised 2026-09-17, explicitly deferred — not scoped or started, just
recorded so it doesn't need rediscovering. The digest now names the
airline and flight number (added same day — `scripts/airlines.py`), which
is enough to go search for the flight yourself; a direct link would skip
that step.

**What's already known, from the "book a real trip" item above:** the
`/aviasales/v3/grouped_prices` response inspected back in step 1's dry run
had a `link` field — an Aviasales deep link with tracking parameters
attached, i.e. already an affiliate link, not something to construct or
register separately. `grouped_prices` isn't wired into `sweep.py` at all
(dropped after step 1 in favour of `prices_cheap` — see `PLAN.md §2` — it
returns "best deal today" data, the wrong shape for a route-matrix sweep).
`prices_cheap`, the endpoint actually swept, was never checked for a
similar field.

**So the real first step here is a live check, not a design decision:**
does `/v1/prices/cheap` carry a link (or enough — origin/destination/
dates/flight number — to build one via Travelpayouts' documented deep-link
tools instead)? If yes, this is mostly plumbing: thread it through
`storage.py`'s schema, `detect.py`'s flag dict, `notify.py`'s render —
the same shape of change as airline/flight_number just was. If no, it
needs either switching the sweep to a link-carrying endpoint (real
architectural cost — re-litigates `PLAN.md §2`'s endpoint choice) or
building links by hand via
[travelpayouts.com/programs/100/tools](https://www.travelpayouts.com/programs/100/tools).
Verify which situation this actually is before assuming either.
