# Phase 1 Plan — Flight Deal Scanner

Status: **awaiting approval**. No code written yet.
Written 2026-08-27, after verifying the API surface against current docs.

---

## 1. Corrections to the brief

Three things in `Brief.md` are out of date. Two are cosmetic; one is architectural.

| Brief says | Actually | Impact |
|---|---|---|
| "~300 requests/minute on the calendar endpoint" | **60/min** default since Jun 2024; `429` + timed block on burst | **Architectural** — see §2 |
| `map.aviasales.com/prices.json` is the best "everywhere" call | Superseded by `/aviasales/v3/grouped_prices` | Endpoint choice; `no_visa`/`need_visa` flags don't survive the move, which affects Phase 2 visa filtering |
| `search_by_price_range` at top level | Moved to `/aviasales/v3/` namespace | Path only |

**Not verified:** actual response shapes. That needs a live token. Handled in §6 step 1 — shapes get confirmed before the storage layer is written, not after.

---

## 2. Endpoint choice — settled empirically against live data (step 1 complete)

The 60/min limit decides the *shape* of the call:

| Approach | Calls | Time @ 60/min |
|---|---|---|
| Per-route (`month-matrix`, one call per origin x dest x month) | ~16,200 | ~4.5 h |
| All-destinations (`destination=-`, one call per origin x month) | 108 | ~2 min |

~150x. The per-route form would run 4.5 hours nightly against a 6-hour job ceiling, with no headroom for retries.

**Which endpoint gives that all-destinations shape was not what the docs suggested.** Both candidates were run live against `LON`/September 2026:

| | `/v1/prices/cheap` (`destination=-`) | `/aviasales/v3/grouped_prices` |
|---|---|---|
| Coverage | **372 destinations, one call** | 85 distinct destinations across 324 date-keys — one fare per *day*, picking the single cheapest route across everywhere combined |
| Verdict | **This is the real "everywhere" endpoint** | Not an everywhere endpoint at all — it's a "best deal today" feed. Wrong shape for a route matrix. Dropped from the sweep. |

**Decision: `/v1/prices/cheap` is the sweep endpoint.** `grouped_prices` isn't used going forward — noted here rather than deleted from memory, in case a future "today's single best deal" feature wants it back.

**Two further things the live response revealed, neither guessable from docs:**

- **No endpoint ever reports which airport a fare departs from — not even when querying the `LON` metro code.** Checked all 372 tickets across all fields: no `origin_airport`, no `origin` field, at all. This broke the original origins decision — see §3.
- **`one_way=true` is silently ignored** on both `/v1/prices/cheap` and `/v1/prices/direct` — tested empirically, not assumed from docs. Every ticket came back round-trip regardless of the parameter. Getting real one-way data would mean a per-route endpoint, i.e. reintroducing the ~16,200-call problem this section exists to avoid. This broke the original trip-type decision — see §3.

**Auth: `X-Access-Token` header, never the `token` query param.** A token in a query string lands in every logged URL, permanently, in a public repo. See §5.

### Call budget, revised for 10 origins (§3) x round-trip only (§3)

- Nightly (months 0-6): `10 x 7` = **70 calls**, ~1.4 min @ 50/min
- Weekly tier (months 7-18): `10 x 12` = **120 calls**
- Worst night (both tiers): **190 calls**, ~3.8 min

Lower than the original 228-call estimate, despite querying 10 origins instead of 6 — dropping one-way outweighs the added London airports.

---

## 3. Decisions locked

| Question | Decision | Why |
|---|---|---|
| Origins | **Revised in step 1.** Query `LHR` `LGW` `STN` `LTN` `LCY` directly, plus `MAN` `BHX` `EDI` `GLA` `BRS` — 10 origins, no metro code | The live response never reports which airport a fare used (§2), so the original "query metro, store the airport returned" plan had nothing to store. Only way to get airport granularity is to make it explicit in the request. Costs 4 extra calls/sweep — trivial |
| Destinations | All via `destination=-`; mark a core subset from `/v1/city-directions` | Curation can't discover the deal you weren't looking for |
| Horizon | Rolling 18mo; months 0-6 nightly, 7-18 weekly | Far months barely move and have thin cache data |
| Trip types | **Revised in step 1.** Round-trip only for Phase 1 — `one_way` is silently ignored by both everywhere-shaped endpoints, confirmed live | Round-trip is what the brief already said families actually book. One-way at full-universe scale needs a per-route endpoint, reintroducing the call-volume problem §2 exists to avoid. Revisit only if a bulk one-way source turns up |
| Detection basis (year one) | Lead time (`depart_date` minus `observed_at`), not calendar-month seasonality — see §4 | Four weeks of history covers many future months once each, never the same month a year apart. A seasonal percentile needs ~12 months minimum; lead-time comparison ("has this dropped against its own recent trend?") is what four weeks can actually support. Additive later, not a rewrite — seasonal comparison gets bolted on as a second detector once a year of history exists, using the same stored columns |

---

## 4. Storage schema

**Layout: append-only daily deltas, compacted periodically — not in-place monthly rewrite.**

Originally planned as one Parquet file per month, rewritten nightly. Dropped: Parquet is compressed binary, so a one-row change produces a globally different file, and git cannot delta that. Rewriting 18 month-files nightly would commit 18 fresh blobs every night regardless of how few rows actually changed — on the order of a gigabyte of git history a year, the exact bloat this design is meant to avoid.

Instead:
- Each sweep writes a small `data/deltas/YYYY-MM-DD.parquet` — only the rows that changed that night, across all months.
- A periodic compaction step (weekly) folds deltas older than a few days into the per-month files under `data/monthly/YYYY-MM.parquet`, then deletes the folded deltas.
- Git stores each small delta once, forever. Only the infrequent compaction commits touch the bulkier monthly files.

Row schema, same for deltas and compacted monthly files — **revised against the real `/v1/prices/cheap` payload confirmed in step 1**, not the originally assumed shape:

```
origin_airport  str    # the literal airport queried ("LGW") — no longer
                        # derived from the response; see §3, origins
destination     str
depart_month    str    # YYYY-MM  <- partition key on the compacted files
trip_type       str    # round_trip only for now; column kept so adding
                        # one-way later (§3) needs no schema migration
price_gbp       float
depart_date     date
return_date     date
flight_number   str
airline         str
expires_at      ts     # cache-freshness signal the API actually returns
observed_at     ts     # when our sweep saw it
```

Two fields from the original draft are gone because the live payload never contains them: **`transfers`** (no such field on any of 372 test tickets) and **`origin_query`** (redundant now that origin is queried as a literal airport, not a metro code needing disambiguation). **`found_at` became `expires_at`** — there is no "when Aviasales cached this" field on this endpoint; `expires_at` is the closest real signal to staleness the response provides, so the schema uses what's actually there rather than a name invented in advance.

**Why this satisfies "cheap percentile query later":** the compacted monthly files stay partitioned by `depart_month`, so scoring reads one file and filters two columns. Deltas exist for git efficiency, not query efficiency — Phase 2 queries compacted files, backfilled with the last few days of uncompacted deltas when it needs to be fully current.

**Change detection:** hash the price-bearing fields per `(origin_airport, destination, depart_date, return_date, trip_type)`; write a row to the day's delta only when the hash differs from the last known value.

**Retention and rollup — keep the shape of the distribution, not its average.** Per the brief: 120 days raw, then roll into summaries. A rollup that keeps only mean and count throws away exactly what a bargain-hunting tool needs — the cheap end of the range — and it can't be reconstructed later once the raw rows are gone. The weekly rollup instead keeps, per `(route, depart_month, lead_time_bucket)`: **min, p05, p25, p50, and observation count.**

**Dedup rule for Phase 2, locked in now:** the cache reflects search *volume*, not availability — a viral fare gets searched thousands of times, and naively counted, would make a real deal look "normal" the more real it is. With one sweep per route per night, `(route, price, date(observed_at))` is already close to naturally deduped; the rule matters more if sweep frequency ever increases, so it's recorded here rather than assumed obvious later.

`expires_at` and `observed_at` answer different questions even though neither is the "when Aviasales first cached this" field originally planned: `expires_at` is the API's own signal for how fresh *this specific* cached price is, `observed_at` is pipeline health — whether our sweep ran and saw data at all. Collapsing them loses the ability to tell a stale API from a broken sweep.

**Note for year one:** the lead-time detection basis locked in §3 needs nothing further from this schema — `depart_date` and `observed_at` are both already columns above, so "days until departure" is just their difference. No extra field, no code decision deferred, nothing to remember to add later.

---

## 5. Lifted from PATTERNS.md

Taken directly: thin YAML / logic in `scripts/`, `workflow_dispatch` alongside cron, off-the-hour cron, step-scoped env-injected secrets, the empty-tree commit guard, `git pull --rebase --autostash`, `timeout-minutes`, single bot identity, UTC-stamped commits, one `requirements.txt`, one pinned Python (3.12), pinned deps.

**Fixed here rather than repeated:**

- **§2 log dump.** The Watchlist prints 2000 chars of raw output to a world-readable log on parse failure. Same trap here is worse — the token is a request parameter. Mitigations: header auth, plus a `redact()` helper every error path goes through, plus truncated dumps to a *file artifact* not stdout.
- **§4.4 silent gaps.** Marked "not yet done" there. Built in from day one: on startup, check the newest `observed_at` and warn loudly if the gap exceeds expected cadence. Every run also logs a one-line heartbeat — rows written, routes covered, cheapest fare seen — committed alongside the data. Four weeks of accumulation with a silent hole is four wasted weeks; a heartbeat makes a broken run visible on day two, not day twenty-eight.
- **§4.3 no failure notification.** README will say to confirm per-account Actions failure email is on.
- **§4.2 unpinned actions.** Recommend pinning to SHA this time. Cheap now, annoying to retrofit.

---

## 6. Build order

Dry-run first, deliberately — the brief lists it as deliverable 6, but it's the thing that de-risks everything else.

1. ~~`config/sweep.yaml` + `--dry-run` against one origin.~~ **Done.** `prices/cheap` vs `grouped_prices` compared against live data; `prices_cheap` won, and the origins/trip-type decisions in §3 were revised based on what the real response actually contained (§2).
2. ~~`scripts/storage.py`~~ **Done.** Verified end-to-end against the real captured step-1 response (change detection, delta write/append, compaction, rollup) — not synthetic data. Two implementation choices the original schema didn't specify, recorded here rather than left implicit in code:
   - **A `data/index/latest.parquet` state file**, committed like everything else, tracks the last known price hash per `(origin_airport, destination, depart_date, return_date, trip_type)`. Needed because a fresh Actions checkout has no other memory of "what changed since last night" — without it, every row would look new on every run, defeating changed-rows-only storage entirely.
   - **`lead_time_bucket` uses 30-day buckets** of `depart_date − observed_at`. PLAN.md §4 said to keep lead time; it didn't say at what resolution. 30 days balances rollup file size against how finely year-one detection (§3) can later distinguish "6 weeks out" from "10 weeks out."
3. ~~`scripts/sweep.py`~~ **Done.** Real sweep loop verified against a live single-origin run (`LHR`, 7 months, 1,505 rows, 0 failures) before touching the full set, per the brief. Idempotency confirmed live too: an immediate re-run against the same data reported `changed=0` across all 1,505 rows. Retry/backoff behaviour (429 + `Retry-After`, persistent 5xx giving up cleanly, non-retryable 401 not retried at all, network errors) verified against simulated failures rather than assumed. One design choice not specified upstream, recorded here: **far-months tier runs on Sundays** (`far_sweep_weekday: 6` in config) — nothing upstream said which day, so this is the concrete pick.
4. ~~`.github/workflows/sweep.yml`~~ **Done.** `37 3 * * *`, `contents: write`, commit guard, rebase, timeout — all lifted from PATTERNS.md as planned. One thing found and fixed only while building this step: `real_sweep()` fetched and ingested data but never called `compact_deltas()`/`rollup_stale()` — if the workflow just ran `sweep.py` nightly, deltas would have accumulated forever with nothing ever folding them, quietly reintroducing the git-bloat problem the whole delta design exists to prevent. Both now run unconditionally at the end of every sweep (no-ops on data too fresh to touch). Also pinned `actions/checkout` and `actions/setup-python` to commit SHAs rather than moving tags — §4.2's fix, applied instead of repeated.
5. ~~`README.md`~~ **Done — Phase 1 complete.** Token setup, the 60-day disable gotcha (resolved, not just noted — see the updated risk above), the per-account failure-email setting, and the "weekly signal, not real-time alert" framing.

Small commits throughout.

---

## 7. Risks

- **The four-week cost of a schema mistake.** A wrong schema isn't found until scoring, by which point the history is wrong. This is why step 1 stops for inspection.
- **60-day inactivity disable — resolved while writing the README (step 5), was an open guess here.** The guess above was backwards: bot/`GITHUB_TOKEN`-authored commits do count as repo activity and reset the clock, confirmed against GitHub's community docs rather than assumed. Since `heartbeat.jsonl` grows on every run, the workflow commits every single night regardless of whether any price changed — in normal operation this is self-sustaining. The real residual gap: a missing/expired token makes `load_token()` fail before anything touches `data/`, so that specific failure produces *no* commit at all — the one path that could compound toward the 60-day disable if unnoticed for two months. README §"Scheduling gotchas" carries this, and recommends turning on GitHub's per-account Actions-failure email as the actual defence.
- **Cache depth on thin routes.** Some `(origin, destination, month)` cells will never get enough observations to support a percentile. Phase 1 should mark observation counts so Phase 2 can refuse to score them, rather than scoring them badly.
- **Data is 2-7 days stale by construction.** Constrains the product promise, exactly as the brief says. A genuine mistake fare dies in hours, well inside that latency, so this can never be a flash-deal alerter — only structural cheapness (capacity dumps, new routes, off-peak) survives long enough to still be true when someone reads the alert. Frame it that way in the README (weekly, no urgency language) before Phase 2 exists to contradict it.
- **Twelve months, not four weeks, until "is this cheap for the season" is answerable.** The four-week accumulation in the brief builds a real baseline for lead-time comparison (§3), but not for seasonal comparison — that needs a full year, because it requires having seen a given month before. Worth saying plainly so year one isn't read as a shortfall against a bar the design never targeted.

---

# Phase 2 Plan — Deal Detection

Status: **built and verified against synthetic scenarios** (2026-08-29). Real
data can't exercise the flag logic yet — see the eligibility note below —
but every branch of the algorithm has been checked against constructed
cases, and the whole pipeline runs clean against real production data,
correctly finding nothing.

Scoped to detection only, not delivery — an explicit choice, not a default:
asked which half of "output" to build first, and detection was picked so
it's ready to tune the moment enough real history exists, rather than
being designed only after the wait is already over.

## What it does

For every flight whose price changed *tonight* (this is deliberately not
"every flight, every night" — a flight whose price didn't move can't
possibly be a new low it wasn't already yesterday, so re-checking it is
wasted work against the brief's "cheap query" goal):

1. **Eligibility gate:** skip it unless it's been observed on at least
   `detection.min_observations` (5) distinct nights. Directly implements
   the plan already recorded in this file's Risks section above — "Phase 1
   should mark observation counts so Phase 2 can refuse to score them."
2. **Flag condition, both required:** tonight's price is a genuine new low
   for that exact flight (same route, same date, same trip type) **and**
   at least `detection.drop_pct_threshold` (15%) below its own recent
   median. Either alone is too weak — "always cheap" would flag forever on
   new-low alone; "slightly cheaper than usual" would flag on the % test
   alone without being a real low.
3. **Output:** `data/flags/YYYY-MM-DD.jsonl`, one line per flag, only
   created on nights with something to say (mirrors `write_delta()`'s
   "empty result is valid, don't write an empty file" convention). Nothing
   gets sent anywhere — this is a record, not an alert.

Verified against constructed scenarios (real data has no eligible route
yet): a genuine 29%-below-median new low flagged correctly; a new low that
was only 12% below median correctly did not flag; a route with a huge drop
but only 3-4 observations correctly did not flag despite the drop size —
the eligibility gate held even under a strong incentive to fire.

## One empirical check done before committing to this design

The whole "compare a fare against its own recent trend" idea only works if
the API keeps returning the *same specific flight* night after night,
rather than jumping between different dates within a queried month. Tested
directly rather than assumed: a fresh live fetch matched an existing index
key on **96% of returned flights**. The signal is real, not a nice idea
sitting on top of noise.

## What changed in storage.py to make this possible

The index (`data/index/latest.parquet`) previously tracked only the latest
price hash per key. It now also tracks `observation_count`, `first_seen`,
`last_seen` — updated for *every* key touched by a sweep, changed or not,
which is what makes "seen on N distinct nights" a real counted fact rather
than a proxy for "the price happened to move N times." An index written
before this existed migrates automatically: missing columns backfill to
`observation_count=1` (conservative — undercounts real history, never
overcounts) rather than requiring a one-off backfill script.

**Real consequence worth knowing:** this was added on 2026-08-29, after
three real sweep nights had already run. The migration doesn't credit
those pre-existing nights — every key's counter effectively restarts from
the day this shipped. The 5-night eligibility gate is about 2-3 days later
in practice than it would have been if this had been built in from day
one. Not a bug, just the honest cost of adding this after the fact rather
than up front.

`storage.load_full_history()` is new too — the first thing that needed a
flight's complete history regardless of whether it's sitting in an
uncompacted delta or an already-folded monthly file, since nothing before
detect.py needed to read both shapes at once.

**A real bug caught by testing before it shipped:** `.values` on a
timezone-aware pandas Series silently strips timezone-awareness — found
because the extended test suite compared `first_seen` against `last_seen`
and got a tz-naive-vs-tz-aware crash, not because it was spotted by
inspection. Fixed by assigning the Series directly instead of `.values`,
and by making every new timestamp column explicitly tz-aware from
construction rather than letting pandas infer a dtype that a later parquet
round-trip could silently degrade.

## What's still open

- **Delivery.** Explicitly deferred — flags are written to a file, read by
  no one yet, its own separate scope. Correction: this originally said
  "Telegram bot per the original brief is the obvious next candidate" —
  overstated. `Brief.md` only ever listed Telegram as one example of
  out-of-scope notification delivery, not a firm commitment, and it's now
  been ruled out as infeasible (2026-08-29). The actual mechanism is an
  open question again.
- **Retention-boundary interaction, unresolved by design.** A far-tier
  flight swept for close to its full ~19-month life could have early
  history rolled into a weekly summary (`data/rollups/`) before it's ever
  close enough to flag — `detect.py` only reads raw history
  (`load_full_history()`), not rollups. For now this just means a shorter
  comparison window for such a flight, not a wrong one. Revisit only if
  this turns out to matter in practice.
- **Tuning `min_observations` and `drop_pct_threshold` against real
  signal** — both are config values precisely so this doesn't need a code
  change once there's enough real history to know whether 5 nights and
  15% are the right numbers.
