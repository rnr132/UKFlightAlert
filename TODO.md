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
`resend`, an API key as the password).

**Done, in order:**
1. ~~Create a free Resend account.~~
2. ~~Add and verify a sending domain~~ — `flightalert.rohit-nair.com`,
   DKIM/SPF confirmed live via DNS lookup, not just Resend's dashboard
   saying so.
3. ~~Update `notify.from_address`~~ — `londondeals@flightalert.
   rohit-nair.com`, matching the "London Flight Deals" rebrand
   (`PLAN.md`, 2026-09-17).
4. ~~Generate an API key, put it in local `.env`.~~
5. ~~`python scripts/notify.py --test <address>`~~ — sent successfully,
   format approved after two redesign passes (HTML, then a decluttering
   fix from direct feedback).
7. ~~Wire `notify.py` into the workflow~~ — runs as its own step every
   night, before the commit (so its heartbeat lands in the same commit as
   the night's data), deliberately not `continue-on-error` so a real
   failure reddens the run instead of hiding.

6. ~~Add `SMTP_PASSWORD` and `NOTIFY_RECIPIENTS` as GitHub Secrets~~ —
   done 2026-09-17, run by hand (not by Claude — moving a live credential
   into GitHub isn't something to automate, same line held on `.env`
   earlier). Confirmed set via `gh secret list` (names/timestamps only,
   values never seen). `smtp_username`/`from_address` are plain config,
   not secrets, so neither needed one.

**This whole checklist is now done.** Every step 1-7 above is complete —
delivery is genuinely production-ready, not just code-ready. The next
real send happens automatically the first time `notify.digest_weekday`
(Friday, moved from Sunday 2026-09-17 — see `config/sweep.yaml`) comes
around on the nightly workflow; nothing further to do
unless the format or recipient list needs changing.

**Worth a deliberate look before that first real Sunday send:** confirm
`NOTIFY_RECIPIENTS` actually holds the intended list — the value moved
into the secret came straight from whatever was in local `.env` at the
time, which may still just be the single test address rather than the
real family/friend list.

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

**Update 2026-09-18 — the live check is done, and there's a real, concrete
path now, not just a fork to resolve later.**

Checked live rather than assumed: a fresh `sweep.py --dry-run` call to
`/v1/prices/cheap` (the endpoint actually swept) returns no `link` field
and nothing booking-related — full field list on a real fare object is
`airline`, `departure_at`, `return_at`, `expires_at`, `price`,
`flight_number`, `duration`, `duration_to`, `duration_back`. So switching
the sweep endpoint isn't the path (that re-litigates `PLAN.md §2`'s
150x-call-volume decision for no real gain) — building a link by hand is.

**The real mechanism, found in Travelpayouts' actual docs, not
guessed:**

1. Build a plain search URL: `https://www.aviasales.com/search/PARAMS`,
   where `PARAMS` is `{ORIGIN}{DDMM depart}{DEST}{DDMM return}{adults}` —
   e.g. `LGW1110KTW14101` for LGW→KTW, depart 11 Oct, return 14 Oct, 1
   adult economy (adult count is mandatory or the link doesn't work;
   full encoding rules — passenger class, multi-city — in
   [Travelpayouts' "Aviasales affiliate links" article](https://support.travelpayouts.com/hc/en-us/articles/5711895629714-Aviasales-affiliate-links)).
2. **That raw URL carries no affiliate credit on its own** — the docs are
   explicit that a manually-built link only starts earning once it's
   converted into a tracked partner link. The programmatic way to do that
   conversion is a real, documented API, not just the dashboard's "create
   link" button:
   `POST https://api.travelpayouts.com/links/v1/create` — batches up to
   10 URLs per call (100 calls/min per marker), takes `trs` (Project ID),
   `marker` (partner ID), and a `links` array of `{url, sub_id}`, returns
   `partner_url` per link. The same account-wide API token already in
   `TRAVELPAYOUTS_TOKEN` authenticates this too — confirmed against
   [the API doc](https://support.travelpayouts.com/hc/en-us/articles/25289759198226-API-for-Travelpayouts-partner-links),
   not assumed from the Data API's own "one token covers everything"
   framing in `README.md`.
3. Thread `partner_url` through the same three places airline/
   flight_number went: `detect.py`'s flag dict, `notify.py`'s render (a
   "Search this fare" link under the price, fitting naturally alongside
   the existing "worth checking live before booking" line — an honest
   framing anyway, since a cached fare isn't guaranteed still bookable at
   that price).

**What's missing before this can actually be built and tested, not
guessed at:** the `trs` (Project ID, subscribed to the Aviasales brand)
and `marker` (partner ID) values — both account-specific, both need to
come from the user's own Travelpayouts dashboard, same as every other
account-specific value in this project (never invented, never assumed).
Ask for both before starting. Building 10-link batches also needs
throttling awareness matching `sweep.py`'s existing rate-limit handling,
since this would run at nightly-sweep scale, not one link at a time.

---

## Personal website: land the project page that's already built

Raised 2026-09-17. A case-study writeup already exists
(`uk-flight-deal-scanner.md`, handed over that session) — first person,
matched to the site's existing voice, built for `rohit-nair.com`'s Astro
content collection at `src/content/experiments/` (a separate repo from
this one). The site's chrome (back link, date line, footer) comes from
its own layout, not from the file's content, so the file is meant to
drop in as-is, frontmatter keys adjusted to match whatever an existing
entry (`credit-watchlist-experiment.md`) actually uses.

**Status as of the last check:** `rohit-nair.com/experiments/
uk-flight-deal-scanner` still 404s — written, not yet landed in the site
repo. This is a "finish wiring in something already built" task, not a
"write it" task if picked up again — check whether the file still exists
wherever it was saved before rebuilding it.

## Website signup form for the weekly digest

Raised 2026-09-17, not scoped or started. The idea: let a visitor to the
personal-site page above add themselves to the digest's recipient list
directly, instead of the current manual process (edit `.env`'s
`NOTIFY_RECIPIENTS` line by hand, then `gh secret set` the whole updated
list — GitHub Secrets are write-only, so `.env` is the only place the
real current list is ever readable again).

**The real constraint to design against, not the mechanism:** the
personal site is a static Astro site with no described backend (checked
while building the page above), and this whole project runs under a hard
zero-cost rule (`Brief.md`). Whatever collects a signup has to clear
both. Not designed yet — worth checking what's actually available before
picking an approach, the same way every other provider choice in this
project got settled (e.g. whether Resend's already-verified domain could
plausibly receive inbound mail too, before assuming a whole separate
service is needed).

## Check GitHub status of this project

Raised 2026-09-17, scope not given — recorded as-is rather than guessed
at. Plausible readings worth checking before starting, not assumed:
repo presentability (description, topics, README polish) ahead of
linking it publicly from the personal site above; GitHub Actions/CI
health specifically (though `data/heartbeat.jsonl` and `data/
notify_heartbeat.jsonl` already cover the pipeline's own health); or
something else entirely. Ask which before acting.
