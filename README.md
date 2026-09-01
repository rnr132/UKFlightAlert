# Flight Deal Scanner

A nightly job that sweeps flight prices from 10 UK airports across a
rolling ~18-month horizon into a growing price history, and flags flights
that have genuinely dropped against their own recent trend. No alerting,
no front end, no per-user config yet — see [Brief.md](Brief.md) for
Phase 1's original scope, and [PLAN.md](PLAN.md) for the architecture and
every decision behind both phases (including several corrections made
after comparing assumptions against the live API).

## What this is, and isn't

- **Not real-time.** The data source is a 2–7 day old cache of other
  people's searches, not live inventory. A genuine mistake fare is gone
  long before this could ever surface it. What survives that latency is
  *structural* cheapness — a capacity dump, a new route, genuine off-peak
  — the kind of thing still true a week later. Any future alerting should
  read as "this route looks unusually cheap right now," weekly cadence,
  never "book this exact seat."
- **Detection compares a flight against its own history, not a season.**
  A route is only scored once it's been observed on 5+ distinct nights,
  and flags only when tonight's price is both a genuine new low and well
  below its own recent median (`scripts/detect.py` — see PLAN.md's Phase 2
  section). Comparing against "is this normal for April" needs having seen
  a previous April — about 12 months of history, not weeks — so that kind
  of seasonal comparison isn't attempted yet.
- **Detection finds; delivery exists but isn't live yet.**
  `scripts/notify.py` builds a weekly email digest of flagged deals — but
  it's deliberately not wired into the nightly workflow, and hasn't sent a
  real email to anyone yet. One test send to a single chosen address
  needs to happen first (see below), same as verifying one origin before
  the full sweep.
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

# Weekly digest email -- NOT wired into the automatic pipeline yet
python scripts/notify.py --dry-run           # build it, print it, send nothing
python scripts/notify.py --test you@x.com    # ONE real email, for format review
```

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
  flags/YYYY-MM-DD.jsonl     deals detect.py found that night — only
                             created on nights with something to say
  heartbeat.jsonl            one line per run: rows fetched/changed,
                             failures, cheapest fare seen, flags found
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
  config.py   shared config/path loading
  sweep.py    fetch, throttle, retry, ingest, heartbeat
  storage.py  normalize, delta write, compaction, rollup
  detect.py   deal detection — flags a genuine new low vs. own history
  notify.py   weekly email digest — built, not yet wired in (see above)
.github/workflows/sweep.yml  the nightly Action
data/                        the accumulating price history (see above)
```
