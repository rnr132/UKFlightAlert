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
3. **`scripts/sweep.py`** — throttle (50/min token bucket), backoff honouring `Retry-After`, redaction, staleness check, nightly heartbeat log. Verified single-origin before full set, per your brief.
4. **`.github/workflows/sweep.yml`** — `37 3 * * *`, `contents: write`, commit guard, rebase, timeout.
5. **`README.md`** — token setup, the 60-day disable gotcha, failure-notification setting, and the product framing below: a weekly "this route looks structurally cheap" signal, not a real-time alert — set that expectation before Phase 2 exists to contradict it.

Small commits throughout.

---

## 7. Risks

- **The four-week cost of a schema mistake.** A wrong schema isn't found until scoring, by which point the history is wrong. This is why step 1 stops for inspection.
- **60-day inactivity disable.** Scheduled workflows are disabled after 60 days of no repo activity, and I do *not* believe pushes made with the default `GITHUB_TOKEN` reset that clock — worth confirming rather than assuming, since silent disablement is the failure mode that looks exactly like success. README will carry the mitigation.
- **Cache depth on thin routes.** Some `(origin, destination, month)` cells will never get enough observations to support a percentile. Phase 1 should mark observation counts so Phase 2 can refuse to score them, rather than scoring them badly.
- **Data is 2-7 days stale by construction.** Constrains the product promise, exactly as the brief says. A genuine mistake fare dies in hours, well inside that latency, so this can never be a flash-deal alerter — only structural cheapness (capacity dumps, new routes, off-peak) survives long enough to still be true when someone reads the alert. Frame it that way in the README (weekly, no urgency language) before Phase 2 exists to contradict it.
- **Twelve months, not four weeks, until "is this cheap for the season" is answerable.** The four-week accumulation in the brief builds a real baseline for lead-time comparison (§3), but not for seasonal comparison — that needs a full year, because it requires having seen a given month before. Worth saying plainly so year one isn't read as a shortfall against a bar the design never targeted.
