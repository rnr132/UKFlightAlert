# Flight Deal Scanner

A nightly job that sweeps flight prices from 10 UK airports across a
rolling ~18-month horizon into a growing price history, then emails a
small list of people the fares that have genuinely dropped against their
own recent trend — but only the two kinds someone can actually act on at
short notice: **Weekend Deals** (a Friday- or Saturday-to-Sunday-or-Monday
trip) and **Holiday Deals** (a trip during, or a couple of days either
side of, a London school holiday). Free to run end to end: GitHub
Actions, the repo as its database, and Resend's free email tier. See
[Brief.md](Brief.md) for the original scope and [PLAN.md](PLAN.md) for the
architecture and every decision behind it, including the many corrections
made after comparing assumptions against live data.

## What this is, and isn't

- **Not real-time.** The data source is a 2–7 day old cache of other
  people's searches, not live inventory. A genuine mistake fare is gone
  long before this could ever surface it. What survives that latency is
  *structural* cheapness — a capacity dump, a new route, genuine off-peak
  — the kind of thing still true a week later. The email says so itself:
  a nightly signal, never "book this exact seat."
- **Detection compares a flight against its own history, not a season.**
  A fare is only considered once it's been seen on 5+ distinct nights. It
  is flagged when tonight's price is a new low for that exact flight
  (within a bounded lookback window) *and* well below its own recent
  median, the trip is the right shape for one of the two products, and it
  departs within a few weeks (`scripts/detect.py` — see PLAN.md's Phase 2
  section for why each rule exists). Every threshold lives in the
  `detection` block of `config/sweep.yaml`. Comparing against "is this
  normal for April" needs having seen a previous April — about 12 months
  of history, not weeks — so that kind of seasonal comparison isn't
  attempted yet.
- **Delivery is live.** `scripts/notify.py` emails a styled digest (via
  [Resend](https://resend.com)) on any night something new clears the
  bar, and nothing on a quiet night. Each recipient gets their own
  separate message, so nobody sees anyone else's address, with a
  Reply-To so people can ask to stop.
- **Joining is manual on purpose.** People sign up at
  [flightalert.rohit-nair.com](https://flightalert.rohit-nair.com) — a
  static page (its own repo) in front of a Google Form. Nothing from it
  reaches this repo automatically: the owner reviews signups and adds
  addresses by hand ([below](#adding-or-removing-a-recipient)), so a
  stranger's submission never becomes a recipient on its own.
- **The repo is the database.** There's no server. Every night's prices
  (and any flags) are committed back into `data/` by the GitHub Action
  itself.

## Setup

### 1. Get a Travelpayouts API token

Register (free) at
[travelpayouts.com/programs/100/tools/api](https://www.travelpayouts.com/programs/100/tools/api).
The token is account-wide — one token covers the Data API and everything
else on the platform — and lives in your **Profile → API token**, not
under any specific tool you have to activate first.

### 2. Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# open .env and paste your token in yourself — never hand it to a script
# or paste it into a chat for someone else to type in for you
```

`sweep.py` reads the token via `TRAVELPAYOUTS_TOKEN` from the environment
— never as a command-line argument, so it can't leak into shell history
or a process listing. Before running anything locally:

```bash
set -a; source .env; set +a
```

### 3. GitHub Actions

The scheduled workflow needs the same token as a **repository secret**,
not committed anywhere:

**Settings → Secrets and variables → Actions → New repository secret**
— name it exactly `TRAVELPAYOUTS_TOKEN`.

Also worth doing once: **Settings (your personal account, not the repo)
→ Notifications → Actions** — turn on email for failed workflow runs.
It's a per-account setting, off by default, and it's the difference
between a broken sweep being visible tomorrow morning versus being
noticed whenever someone happens to check the Actions tab.

The repo must be **public** for this to run on free, unlimited Actions
minutes — see [Brief.md](Brief.md) for why.

### 4. Email secrets

The workflow's "Send nightly digest" step runs every night and sends only
when either product has something new that hasn't already been emailed
that day — a quiet night exits cleanly with nothing to do. It reads three
more repository secrets, same place as above:

- `SMTP_PASSWORD` — a [Resend](https://resend.com) API key. The SMTP
  username and sender address aren't secrets, so they live in
  `config/sweep.yaml`'s `notify` block instead (see `PLAN.md`'s delivery
  section for why Resend, not Gmail/Yahoo).
- `NOTIFY_RECIPIENTS` — comma-separated real email addresses. Never
  committed anywhere, for the obvious reason.
- `REPLY_TO` — optional but recommended: the one inbox that replies and
  "say stop" requests land in. The sending domain can't receive mail
  (`flightalert.rohit-nair.com` points at the signup page), so without
  this a reply goes nowhere and digests carry no unsubscribe line.
  Recipients can see this address in the Reply-To header, so use one
  you're comfortable showing them.

If `SMTP_PASSWORD` or `NOTIFY_RECIPIENTS` is missing on a night there's
something to send, the step fails loudly (credentials are checked at
startup, not partway through an SMTP call) — deliberate: a silent skip
would be worse than a red run, same reasoning as the Actions-failure
email above. A missing `REPLY_TO` only prints a warning, since it
shouldn't be able to stop the digest.

#### Adding or removing a recipient

GitHub Secrets are write-only — once set, a value can't be read back — so
your local `.env` is the only readable copy of the list. To change it:

1. Edit the `NOTIFY_RECIPIENTS=` line in `.env` (comma-separated; spaces
   after commas are fine). Duplicates and malformed entries are dropped
   at send time, with a count — never the addresses — in the run log.
2. Push it to the secret yourself, from the project folder:
   ```bash
   gh secret set NOTIFY_RECIPIENTS --body "$(grep '^NOTIFY_RECIPIENTS=' .env | cut -d= -f2-)"
   ```
3. Confirm with `gh secret list` — the updated time should be now. No
   restart is needed; the next run reads the secret fresh.

A "stop" reply is honoured the same way: delete the address and repeat
steps 2–3. Moving credentials into GitHub is deliberately a manual,
human step — nothing in this repo does it for you.

## Running it

```bash
# Inspect a live response without writing anything
python scripts/sweep.py --dry-run --origin LHR

# Real sweep, one origin only — verify before widening, per the brief
python scripts/sweep.py --origin LHR

# Real sweep, every origin in config/sweep.yaml — what the Action runs nightly
python scripts/sweep.py

# Maintenance + detection (also run automatically at the end of every sweep)
python scripts/storage.py --stats
python scripts/storage.py --compact --rollup
python scripts/detect.py --date 2026-09-15   # re-check a specific past night
python scripts/detect.py --product weekend   # just one of the two products

# Nightly digest email -- also runs automatically as part of the nightly
# workflow; needs SMTP_PASSWORD/NOTIFY_RECIPIENTS (and ideally REPLY_TO)
python scripts/notify.py --dry-run                     # build it, print it, send nothing
python scripts/notify.py --dry-run --date 2026-10-02   # preview a past night that had flags
python scripts/notify.py --test you@x.com              # ONE real email, for format review
python scripts/notify.py --date 2026-10-02 --resend    # replay a night, even fares already sent
python scripts/notify.py --owner-report                # print the weekly owner summary (sends nothing)

# Preview a detection change against real recent nights before making it
python scripts/replay.py
python scripts/replay.py --set detection.drop_pct_threshold=0.25
```

## Tuning a threshold safely

Every detection threshold lives in the `detection` block of
`config/sweep.yaml`. Each past change (the drop threshold has been through
15% → 30% → 20%, plus the trip-shape rules and the 21-day cap) was judged
by reading the emails it produced *after* they went out — fine for one
recipient, not once strangers are on the list. So preview first:

```bash
python scripts/replay.py --set detection.drop_pct_threshold=0.25
```

It re-runs detection against the real nights still on disk under that
value, prints what would have flagged next to what the current settings
flag, and shows where every other fare dropped out (the funnel: changed →
right shape → soon enough → seen enough nights → has history → new low →
far enough below typical → flagged). It is strictly read-only and refuses
a setting name it doesn't recognise. It's a preview, not a measurement:
only the last few nights still exist as raw deltas, and they're judged
against today's index (the script's docstring spells out how).

The same funnel is recorded in `data/heartbeat.jsonl` every night, and
every Monday the digest step also emails the **owner** (the `REPLY_TO`
inbox, never the recipient list) a summary: sweep health, who was sent
what, and that funnel summed over the week with the tightest tunable gate
named. The report is sent by the same nightly job it describes, so a
Monday with no report is itself the alarm — check the Actions tab.

A sweep exits non-zero if any origin-month call ultimately failed after
retries — but whatever it *did* successfully fetch still gets committed
by the workflow regardless (`if: !cancelled()` on the commit step), so a
bad night shows red without losing the data a good night's worth of calls
still produced.

## Data layout

```
data/
  deltas/YYYY-MM-DD.parquet  one small file per night — only rows whose
                             price actually changed since last seen
  monthly/YYYY-MM.parquet    deltas older than a few days, folded in —
                             full-resolution history, one file per
                             depart month, partitioned for cheap queries
  rollups/YYYY-MM.parquet    rows older than retention.raw_days (120),
                             collapsed to weekly min/p05/p25/p50/count —
                             never mean-only, the cheap end of the
                             distribution is the whole point
  index/latest.parquet       last-known price hash + observation count per
                             route — how a fresh checkout knows both "what
                             changed" and "is this route eligible to score"
  flags/<product>/YYYY-MM-DD.jsonl
                             deals detect.py found that night, in a
                             weekend/ or holiday/ folder — only created
                             on nights with something to say
  heartbeat.jsonl            one line per run: rows fetched/changed,
                             failures, cheapest fare seen, flags found,
                             and the per-product funnel (where that
                             night's changed fares dropped out)
  notify_heartbeat.jsonl     one line per real notify.py run (not
                             --dry-run/--test): sent or skipped, why, how
                             many were delivered or failed, and which
                             fares went out (so a same-day re-run can't
                             email them twice). Never any addresses — the
                             same silent-gap protection as
                             heartbeat.jsonl, for the digest send
```

Deltas exist because Parquet is compressed binary — a one-row change
produces a globally different file, so git can't meaningfully delta it.
Small nightly files keep git history small; monthly files keep queries
cheap. Full reasoning in [PLAN.md §4](PLAN.md).

## Scheduling gotchas

- **Cron is UTC always,** and doesn't observe BST — the sweep runs at a
  fixed UTC time (`37 3 * * *`) year-round, an off-the-hour minute since
  GitHub's scheduler is most congested at `:00`.
- **Scheduled workflows disable themselves after 60 days with zero
  commits to the repo.** In normal operation this shouldn't bite: the
  heartbeat file grows on *every* run, so the workflow commits every
  single night regardless of whether any price changed, which is itself
  activity that resets the clock — confirmed against GitHub's community
  docs, not assumed. The one gap: if `TRAVELPAYOUTS_TOKEN` is missing or
  expired, the script fails before writing anything to `data/`, so
  *that specific night produces no commit at all* — the one failure mode
  that could compound toward the 60-day disable if it went unnoticed for
  two months straight. The Actions-failure-email setting above is the
  real defence here; if the schedule ever does get disabled, re-enabling
  it is a single click on the workflow's page in the Actions tab.
- `workflow_dispatch` is always available on the workflow's Actions page
  for replaying a missed night by hand, independent of the schedule.

## Tests

```bash
pytest -q        # from the repo root; about a second
```

Around 125 hermetic tests: SMTP is faked, `data/` is replaced by a
throwaway store, and nothing needs a secret or the network — they cannot
send mail or spend an API call. They cover the rules that decide what real
people get emailed (trip shapes, the lead-time cap, the new-low window,
per-product flagged prices), the sending path (one message per recipient,
no address in a log or in another recipient's inbox, safe to re-run),
storage's index bookkeeping, booking links, the school-holiday calendar,
and the real config file and workflow (for example, that the test step
sits *before* the digest step).

The nightly workflow runs them between the sweep and the digest. A red
test blocks that night's email, not that night's data — the sweep's
results are still committed. After fixing the cause, replay the digest by
hand (`workflow_dispatch`): that night's flags are on disk, but a
single-night digest won't pick them up tomorrow by itself.

The suite was checked the way it matters — by breaking the code on
purpose (16 deliberate breakages across the rules, storage, booking
links, workflow and config) and confirming each one fails a test, not just
by passing against working code. What it can't tell you is whether a
*threshold* is a good choice; that's what `scripts/replay.py` is for.

## Keeping the workflows current

Every `uses:` in `.github/workflows/` is pinned to a full commit SHA, not
a moving tag — a compromised upstream tag then can't roll into a run on
its own ([PLAN.md §4.2](PLAN.md)). The trade-off is that updates have to
be pulled in by hand. `check-action-pins.yml` runs `check_action_pins.py`
once a quarter (and on demand): if any pinned action is behind its latest
release it opens a single `action-pins` issue with the exact old → new
SHA to write. An all-current run does nothing. When the issue appears,
re-resolve the SHAs independently (the issue body carries the
`git ls-remote` command), skim the release notes, bump the pins, close
the issue.

## Repo structure

```
Brief.md                     original scope and constraints
PATTERNS.md                  patterns lifted from a prior project's automation,
                             and its known rough edges — read before this repo
                             existed, per the brief's own request
PLAN.md                      architecture, every decision and why, what changed
                             after comparing assumptions against live data
config/sweep.yaml            origins, horizon, endpoint, retention — no secrets
scripts/
  config.py             shared config/path loading
  sweep.py              fetch, throttle, retry, ingest, detect, heartbeat
  storage.py            normalize, delta write, compaction, rollup
  detect.py             deal detection — the two products' rules, vs. own history
  replay.py             read-only "what would this have flagged?" preview for
                        any detection setting, against real recent nights
  booking_links.py      builds Aviasales search links and converts them to
                        tracked partner links (the one network call outside
                        sweep.py's fetch)
  notify.py             nightly email digest, one message per recipient,
                        wired into the nightly Action (see above)
  places.py             airport code -> city/country/continent (offline)
  airlines.py           airline code -> name (offline; vendored CSV)
  school_holidays.py    London school-holiday windows the Holiday product uses
  check_action_pins.py  quarterly: is any SHA-pinned action behind its latest
                        release? opens a tracking issue if so (stdlib only)
tests/                       pytest suite — `pytest` from the repo root
.github/workflows/
  sweep.yml             the nightly Action
  check-action-pins.yml runs check_action_pins.py quarterly + on demand
data/                        the accumulating price history (see above)
```
