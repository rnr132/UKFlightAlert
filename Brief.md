# Flight Deal Scanner — Phase 1 Brief

## What I'm building

A cheap-flight detector for my family and friends. It sweeps flight prices from UK airports across a rolling 18-month horizon, builds a price history, and later alerts people when a fare is anomalously cheap for that route and month.

It's a hobby project. Two hard constraints:

1. **Running cost must be effectively zero.** Free tiers only. No paid hosting, no paid database, no paid API.
2. **It must scale to ~50 users without scaling cost.** Price data is keyed on `(origin, destination, month)` and is user-independent. Sweep once into a shared dataset; treat each user's preferences as a filter applied afterwards. Never scan per user.

I am UK-based, so: GBP, UK origin airports, British passport assumptions later on.

## Before you write any code

I have an existing repo called **The Watchlist** — a daily credit-news digest built on GitHub Actions + GitHub Pages + the Anthropic API. I've already debugged the awkward parts there: dotfile handling, JSON parsing failures, workflow scheduling, and API cost overruns.

**Read that repo first.** Tell me which patterns are worth lifting directly — the workflow YAML structure, secrets handling, the commit-data-back-to-repo step, and anything you spot that I got wrong there and shouldn't repeat. I'd rather start from a scaffold I've already made work than from scratch.

Then propose a plan and wait for me to approve it before writing code.

## Phase 1 scope — build exactly this and nothing more

A nightly job that pulls flight prices into storage. **No alerting, no users, no front end.** The system should run silently for four weeks accumulating history, because "cheap" is meaningless without a baseline.

Deliverables:

1. **`config/sweep.yaml`** — origins, destination universe, horizon, currency, market, sweep cadence
2. **`scripts/sweep.py`** — pulls prices, normalises, writes to storage
3. **`scripts/storage.py`** — read/write layer with the retention policy below
4. **`.github/workflows/sweep.yml`** — scheduled Action
5. **`README.md`** — setup steps, including how to get and set the API token
6. A `--dry-run` flag that hits the API but writes nothing, so I can inspect the response shape

## Data source

**Travelpayouts / Aviasales Data API.** Free to register, affiliate-monetised rather than per-call.

Important: this is a *cache* of real user searches, not live inventory. Results are 2–7 days old depending on endpoint. The real-time Search API requires 50,000 MAU, which I don't have and won't. So the product promise is "we spot anomalies and point you at them," never "book this exact seat."

Endpoints I believe are relevant — **verify all of these against current docs rather than trusting me, as this API surface has shifted recently**:

- `search_by_price_range` — accepts `destination=-` for all routes
- `prices_for_dates` — cheapest tickets for given dates
- `v2/prices/month-matrix` — prices across a month
- `map.aviasales.com/prices.json` — takes `origin_iata`, period, price ceiling, `min_trip_duration_in_days` / `max_trip_duration_in_days`, and `no_visa` / `need_visa` flags

That last one looks like the most efficient "everywhere from London" call and the visa flags may be useful later. Work out which endpoint gives the best coverage-per-call and tell me why you chose it.

Rate limit is around 300 requests/minute on the calendar endpoint. Build in throttling and retry-with-backoff from the start.

## Architecture constraints

**Public repo.** GitHub Actions minutes are unlimited for public repos and capped for private ones. Token lives in GitHub Secrets and must never appear in source, logs, or committed data.

**Storage: the repo is the database, but carefully.** Six origins × ~150 destinations × 18 months is roughly 16,000 rows per sweep. Naive daily CSV snapshots would be hundreds of MB a year of git history and would eventually make the Action time out. So:

- Write only *changed* prices, not full snapshots
- Compressed Parquet, one file per month, not one appended file
- Keep 120 days of raw data; roll older data into weekly summaries
- Design the schema so the eventual percentile-scoring step is a cheap query

If you think a different approach is better, say so before building — but the constraint is free and low-maintenance, so Cloudflare D1 is the only paid-tier-adjacent option I'd consider, and only later.

**No LLM calls anywhere in the sweep path.** Ever. That's how I burned money on The Watchlist. Deal detection will be pure arithmetic. An LLM may write digests later, and only for already-flagged candidates.

## Scheduling gotchas to handle

- GitHub Actions scheduled workflows are **disabled automatically after 60 days of repo inactivity**. Note this in the README and suggest a mitigation.
- Cron on the hour is heavily contended and runs late. Pick an odd minute.
- The workflow needs `permissions: contents: write` to commit data back.
- The commit step must not fail when there's no diff.

## Explicitly out of scope for Phase 1

Do not build any of these yet, even if they seem quick:

- Telegram bot or any notification delivery
- Percentile scoring, thresholds, or deal detection
- User signup, Google Form ingestion, or per-user config
- GitHub Pages front end
- Visa filtering or school-holiday logic
- Any Anthropic API integration

## How I want you to work

- Plan first, confirm with me, then build.
- Small commits with clear messages.
- Verify the sweep works against a single origin before running the full set.
- If the API response doesn't match the docs, tell me rather than working around it silently.
- Flag anything where you think I've made the wrong call.

## Open questions — raise these with me before or during the plan

1. Which origins to start with? My instinct is `LON` as a metro code plus `MAN`, `BHX`, `EDI`, `GLA`, `BRS` — but if the metro code hides cheap fares from specific London airports, tell me.
2. Is a curated destination universe better than taking whatever the "all destinations" endpoint returns, and if curated, how do we pick it?
3. Should the horizon be a rolling 18 months from today, or fixed calendar months?
4. Round-trip only, or one-way too? Round-trip is what families actually book, but one-way data may be denser.
