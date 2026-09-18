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

## A second real bug, found only after real flags fired (2026-08-31)

`observation_count` incremented once per **sweep run**, not once per
**calendar night**. On 2026-08-29, three runs happened on the same UTC day
— one scheduled, plus two of mine (a local test and a manual
`workflow_dispatch`, both while building this feature) — and every LHR key
that already existed got incremented twice for what was really one day.
Two routes (`LHR→YOW`, `LHR→VLC`) rode that inflation to `observation_count:
5` on 2026-08-31 and flagged: both real 15%+ drops, but on a baseline that
was really 4 independent nights, not 5.

**Fix:** `filter_changed()` now checks whether a key's `last_seen` is
already stamped with today's date before incrementing — a same-day rerun
(retry, manual verification) no longer double-counts. Verified against the
exact real scenario (three timestamps on one day, then a genuine next day)
before trusting it.

**Correcting the already-inflated data was only possible approximately, and
that limit is worth recording plainly.** The two flagged keys were
verifiably part of the original migration baseline (`first_seen: NaT`), so
decrementing them from 5 to 4 is exact. But finding *every* similarly
affected key ran into a real structural limit: changed-rows-only storage
never records "checked, price unchanged" — only price changes get written
anywhere — so there's no stored trace of which specific keys were touched
by *both* Aug-29 runs versus touched by only one, or not at all that day.
Both produce indistinguishable final counts. Resolved in the safe
direction rather than left unresolved: every LHR baseline key
(`first_seen.isna()`) had its count decremented by 1, floored at 1 (so
keys never touched since the baseline, correctly still at 1, are
untouched). This may delay a handful of genuinely-unaffected routes by one
extra night before they're eligible — a small, harmless cost, chosen
deliberately over the alternative of leaving some routes wrongly eligible
a night early, which is the actual failure this whole fix exists to
prevent. 1,808 LHR keys were touched by the correction; 899 of them had
already reached `observation_count: 5` and are now correctly back at 4.

## detect.py: don't re-flag a route that only ties its own last flag (2026-09-05)

Found by checking observation counts, not by a report of anything broken:
`LCY→BRE` flagged on both 2026-09-03 and 2026-09-04 at the identical
£239 — because `_price_hash()` includes flight number and airline, a
*different* flight now selling the same fare still counts as "changed"
and gets re-evaluated, and the flag condition was "price ≤ historical
min," which a tied price satisfies. Harmless for storage (a genuinely
different flight is worth recording), wrong for detection — the same
route would have shown up twice in one weekly digest looking like a
copy-paste error.

**Fix:** the index gained a `flagged_min_price` column, stamped with the
price whenever a route flags. A route only re-flags if it beats that
price — strictly, not ties it. `storage.load_index()`'s migration path
backfills `NaN` (never flagged) for any index written before this column
existed, consistent with how `observation_count`'s own migration works.

**Corrected the real historical record, not just the code, since nothing
had been emailed yet:** replayed all 55 real flags in chronological order
against the corrected rule. 54 would still have fired; the Sept-4 BRE
repeat wouldn't have, so it was removed from `data/flags/2026-09-04.jsonl`
and `flagged_min_price` was backfilled for all 54 genuine keys from their
real flag history — not just reset to NaN and left to re-derive slowly,
which would have let BRE (and anything else already flagged at its true
best price) re-flag once more on the next unrelated flight-number change
before the fix could take effect.

Verified against the exact reproduction before touching real data: first
genuine drop flags and records the price; the same price under a
different flight number does not re-flag; a genuine further drop does,
and updates the recorded price down again.

**The fix didn't survive contact with a real second run, and only
re-verifying on real infrastructure caught it.** `filter_changed()`
rebuilds an index row for *every* key touched by a sweep — needed so
`observation_count`/`last_seen` stay correct whether the price changed or
not — but that rebuild only carried forward the columns it already knew
about. `flagged_min_price` was added to `load_index()`'s schema but never
added to this reconstruction, so the very next time *any* sweep touched an
already-flagged key (which is virtually guaranteed — `destination=-`
returns most of the route universe on every call), its `flagged_min_price`
silently reset to NaN. A manual verification run right after deploying the
first fix reproduced exactly the bug the fix was meant to prevent: 20 of
the 54 backfilled keys got wiped and immediately re-flagged at their tied
price, plus the already-fixed BRE case came back too, for 21 fresh
duplicates in one run. The unit tests passed throughout, because none of
them simulated a key being touched by an *unrelated* ingest between being
flagged and being re-evaluated — a real gap in test coverage, not a flaw
in the tests that existed.

**Real fix:** `flagged_min_price` added to `filter_changed()`'s merge and
carried forward unchanged in the rebuilt row — it's detect.py's state, not
something this function should ever modify. A regression test now
specifically reproduces "flagged, then touched again by an unrelated
ingest" and confirms the value survives.

**Corrected the real data a second time**, more thoroughly this time:
rebuilt the entire genuine-flag picture from scratch by replaying all 83
accumulated flag entries (55 original + 29 from the buggy verification
run) in chronological order — 62 genuine events across 61 distinct keys
survived, 21 redundant entries removed, `flagged_min_price` rebuilt
cleanly from that corrected history rather than patched incrementally.

## Delivery — email digest (scripts/notify.py, 2026-09-01)

Built after Telegram was ruled out and email was chosen directly (no
Discord group already in use; WhatsApp's official API breaks the brief's
zero-cost constraint). Weekly, not per-night — the project had already
committed to "weekly signal, not real-time alert, no urgency language" in
README.md and this section before any delivery mechanism existed, and a
per-night email would have quietly contradicted that.

- Reads the last 7 days of `data/flags/*.jsonl`, plain-text template, no
  LLM involved anywhere.
- Skips silently on a non-digest-day or a quiet week — same convention as
  `write_delta()`/`write_flags()`.
- `notify.digest_weekday` matches `far_sweep_weekday` (Sunday) — one
  predictable weekly rhythm for the whole system rather than two.
- Recipients and the SMTP password are env vars only
  (`SMTP_PASSWORD`/`NOTIFY_RECIPIENTS` — see the 2026-09-17 entry below for
  why `SMTP_USER` is gone) — real people's email addresses can't live in
  this public repo's committed config, unlike everything in
  `config/sweep.yaml` so far.

**Verified without ever sending a real email:** the 7-day window
correctly excludes a flag from 9 days back while including ones from
4-6 days back; missing credentials and empty recipients both fail loudly
before any SMTP connection is attempted, not partway through; a
non-digest-day with `--force` still builds the digest correctly.

**Deliberately not wired into the automatic pipeline yet.** Per the plan
agreed before building: one real test email needs to go to an address the
user chooses (most likely their own) for format review — the same
"verify against one origin before the full set" principle applied to
delivery — before this touches the real recipient list or the nightly
workflow.

**Reworked 2026-09-10, after the first real week put 140 fares in the
digest at the 15% bar.** `drop_pct_threshold` raised to 0.20 (detect.py
picks it up for future flagging; `build_digest()` also filters to the
same bar, so it reshaped the existing record straight away — 140 → ~80 in
the 7-day window). The digest now groups by the destination's continent,
sorts by drop size (largest first) within and across groups, and leads
each entry with the place in English — `scripts/places.py` resolves an
IATA code to `City, Country` via `airportsdata` + `pycountry_convert`,
both bundled offline data so the no-network rule still holds; three metro
codes (`BUH`, `TCI`, `BAK`) need a manual override, unknowns fall through
to the raw code under "Other" rather than crashing. Then `min_trip_nights` (2) added on 2026-09-10 as well — same-day and
next-day round trips aren't leisure fares, so `detect.py` skips them and
`build_digest()` filters them out of the existing record (11 flags,
including what had been the 62% Geneva headline, a 1-night trip). Then grouped region -> destination (2026-09-10): every flagged fare to
one place sits under a single heading, repeat flags of the same itinerary
collapsed to the latest/lowest, dates shown as `1-Oct-26`, trip length in
place of the redundant `round_trip` label. Still likely needs a hard entry
cap or per-recipient region filtering before it goes live — noted, not built.
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

## Delivery sender: Gmail/Yahoo search abandoned, Resend chosen (2026-09-17)

The provider search recorded above (2026-09-01) picked Yahoo as the sender
because it had no *structural* blocker, only an aging wait. That wait
turned out to be the wrong axis to have optimized for. Two different
anti-abuse gates were hit back to back in practice: Gmail rate-limited
*creating* a new account after several attempts in one week (TODO.md,
observed before 2026-09-01), then Yahoo's fraud heuristic on *app-password
issuance* for a new-looking account stayed unresolved for two-plus weeks
with no published timeframe and no way to check progress — still stuck
when raised again on 2026-09-17.

Both gates are checking the same thing: is this a human signing up for a
mailbox. That was never the actual shape of the problem — a script
emailing a fixed small list weekly isn't a human opening an inbox, and
every consumer webmail provider's fraud model is tuned to be suspicious of
exactly that mismatch. The fix isn't a more patient wait for a better
consumer account; it's a tool built for programmatic sending in the first
place.

**Switched to Resend** (free transactional-email tier: 3,000/month,
100/day, SMTP relay included, no card required). Checked live against
Resend's current SMTP docs rather than assumed: host `smtp.resend.com`,
port 587 with STARTTLS — the same connection shape `send_email()` already
used, so the TLS handshake code is untouched. One real incompatibility
with the old Gmail-shaped code: Resend's SMTP username is always the
literal string `resend`, decoupled entirely from the `From:` address,
whereas the Gmail model conflated "login identity" and "sender identity"
into a single value (`SMTP_USER`, used for both). `notify.py`,
`config/sweep.yaml`, and `.env.example` are updated for this split:
`smtp_username` and `from_address` are now separate config fields, and
since neither is secret (the from address is the visible sender on every
email regardless), only the API key stays in `SMTP_PASSWORD`.
`sender_env_var`/`SMTP_USER` are retired — nothing meaningful belongs in
them under this model.

The trade for losing Yahoo's "no structural blocker, just wait" property:
Resend needs a verified sending domain, i.e. one-time DNS changes on
`rohit-nair.com` — see TODO.md for the exact remaining steps. Slower to
describe than "wait," but bounded and self-directed rather than an
unbounded wait on someone else's fraud model — and it was available the
entire time the Yahoo wait was running.

**Not verified end-to-end with a real send yet.** That needs the domain
verification and an API key, both human steps — account creation and DNS
edits aren't things to automate, so they're recorded as the remaining
checklist in TODO.md rather than done here. `notify.py`'s send-path logic
otherwise carries forward unchanged from its 2026-09-01 verification (same
`smtplib`/STARTTLS flow, same fail-loudly-on-missing-credential guard at
startup) — only the provider-specific values moved.

**Update, same day:** domain verified, API key generated, real test send
succeeded on the first attempt — full chain (Namecheap DNS → Resend →
SMTP relay → inbox) confirmed live, not just mocked.

## Digest redesign: HTML + 25% bar (2026-09-17)

The first real test send (plain text, 20% bar) landed with 47 flags / 43
unique itineraries — too plain and too long. Two changes, both requested
directly rather than inferred:

**1. Threshold 0.20 → 0.25.** Checked against the real 7-day window
before picking a number rather than guessing: 47 flags/43 unique at 20%,
31/29 at 25%, 16/16 at 30%, 11/11 at 35%. 25% was the ask; the others are
recorded here in case 29 still reads as too many once seen rendered.
`_prepare_digest()`'s filtering is unchanged in mechanism from the
2026-09-10 change — config value only, reshapes the existing record at
render time, no data migration.

**2. Plain text → HTML, with a text/plain part kept alongside it, not
replaced.** `send_email()` now sends `multipart/alternative`
(`text/plain` first, `text/html` second — RFC 2046 has the client render
the *last* part it understands, so this prefers HTML where supported and
still degrades cleanly on clients/screen readers/settings that don't want
it). Layout: table-based with inline styles throughout, not CSS
grid/flexbox — the one layout approach that survives Outlook's Word
rendering engine as well as Gmail and phone mail apps. No remote images or
web fonts, so nothing for image-blocking to break and nothing to fail to
load; a system font stack renders natively everywhere instead. Deal cards
grouped region → destination → fares, same tree `_prepare_digest()` already
built for the text version — one shared data-preparation function feeding
two renderers, so text and HTML can never disagree about which fares
qualify. Drop-% badges are tier-coloured (blue 25–34%, green 35–49%, amber
"🔥" 50%+) so magnitude reads at a glance, not just from the number.

**A real bug found only by looking at it, not by reading the code:** the
first render used a hard-coded `width="600"` table. Fine on a wide preview
pane; on a real phone width (checked at 375px, the size that matters most
since most email gets read on a phone) it overflowed instead of shrinking,
cutting the discount badge off the right edge entirely behind a horizontal
scrollbar. Fixed to `width="100%"` with `max-width:600px` — fluid up to a
cap rather than fixed — and re-checked at both 375px and desktop width
before trusting it. This is exactly why the visual check happened in an
actual browser rather than from reading the generated markup.

**A second, smaller bug fixed in the same pass, found while refactoring
rather than by symptom:** the subject line and `flags_count` used
`len(flags)` — every flag record read from the 7-day window — not the
count of unique itineraries actually shown after filtering and collapsing
repeats. Harmless while the threshold was stable (today's 43 unique
happened to equal 43... actually equalled 47 raw by coincidence of a
stable bar), but silently wrong the moment the threshold changes: old
flags written under a lower bar stay in the JSONL files and would still
be counted in the subject even after no longer clearing the digest's own
bar. `_prepare_digest()` now returns the shown count directly and every
caller (subject line, "N fares" line in both renderers, `run()`'s return
dict) uses that, not a raw flags count.

**Verified:** real 7-day data end to end at the new bar (29 unique
itineraries, matches the pre-computed number above) via `--dry-run`;
non-ASCII place names (e.g. "Türkiye") and the unresolved-destination
fallback (bare code, no dangling formatting — PLAN.md's "Other" case)
both render correctly; the MIME message was parsed back apart (not just
grepped as raw text, which broke on quoted/base64 transfer-encoding) to
confirm true `multipart/alternative` structure, correct part order, and
utf-8 survival; mocked-SMTP login/from/to unchanged from the Resend
verification above. Not yet sent as a real email in this form — that's
next, pending a look at the rendered preview.

## Three more, same day (2026-09-17): 25% → 30%, school-holiday tag, rebrand

**Threshold 0.25 → 0.30**, requested directly rather than off another
volume complaint. Real 7-day window at the time: 16 flags / 16 unique —
already fully unique, nothing left to collapse at this bar (30%+ drops
are rare enough that repeat-flagging the same itinerary within a week
essentially stops happening). 35% was also checked and recorded (11/11)
in case 30 still isn't the number once seen rendered.

**New: `scripts/school_holidays.py`.** Tags a fare when its
`[depart_date, return_date]` falls within ±2 days of a London school
holiday — shown as a small violet pill under the route/dates line,
deliberately a different colour from the drop-% badge so it reads as a
different *kind* of signal (timing, not price). There is no single
"London" term-date calendar — England's ~33 boroughs, plus academies, each
set their own, typically within a few days of one another — so this is
one common-case estimate, which is what was actually asked for ("most
likely"), not a per-school lookup.
- **2026-27 school year: sourced from real, currently-published borough
  term dates** (Bexley, Greenwich, Lambeth, Harrow, Hounslow, Tower
  Hamlets — checked live; they converged on the same or near-identical
  dates rather than needing to be reconciled). Each holiday window is the
  *full calendar gap* between terms (weekends included, not just the
  Mon–Fri closed days) — the more honest definition of "on holiday," and
  it hands the ±2-day tolerance a more generous edge on top.
- **2027-28 school year: extrapolated**, not sourced — no borough
  publishes that far out yet (typically 1-2 years ahead only). Same
  seasonal pattern as 2026-27, weekday-verified so term boundaries land on
  the right days of the week, but flagged inline in the module and here as
  an estimate to revisit once real dates exist.
- Checked against real flag data, both directions: Harare's 13–30 Oct trip
  correctly tags "October half-term"; Faro's 4–11 Nov trip — one day past
  the tolerance window's end (half-term's calendar gap ends 1 Nov, +2 days
  = 3 Nov) — correctly does *not* tag. The boundary is precise, not just
  roughly right.

**Rebrand: "Flight Deal Scanner" → "London Flight Deals" in every
recipient-facing string** (subject line, HTML `<title>`, in-body header,
text-digest header) **and the sender identity** (`from_address` →
`londondeals@flightalert.rohit-nair.com`, new `from_name: "London Flight
Deals"` sent via `email.utils.formataddr` — not hand-built string
interpolation, so a display name with a space is always encoded
correctly). The project's own internal name (this repo, `PLAN.md`,
`README.md`) is untouched — that's a separate thing from what a recipient
sees in their inbox, and only the latter was asked for. Domain-level
DKIM/SPF verification authorizes any local part at `flightalert.
rohit-nair.com`, so the address change needed no new DNS work — confirmed
by construction, to be reconfirmed by the next real test send.

**A judgment call, flagged rather than assumed:** the in-body HTML/text
header was changed to match the new sender name even though only "the
name from which the emails come" was asked for — a `From: London Flight
Deals` envelope opening into a body still headed "Flight Deal Scanner"
would read as a mismatch, not a deliberate two-name design. Easy to
revert if the internal project name was meant to stay visible in the body.

**Verified:** real data end to end at 30% (16, matches the pre-check);
`From:` header parsed back apart to confirm `"London Flight Deals"
<londondeals@flightalert.rohit-nair.com>` — display name in the header,
bare address still passed to `sendmail()` as the SMTP envelope sender,
which are two different things and easy to conflate; browser-checked at
375px again after the changes (not assumed still fine from the last
check). Not yet sent as a real email — next.

**Refined same day: "near {holiday}" → during/before/after.**
`school_holidays.nearby()` now classifies the relation, not just
proximity — "during" is a genuine overlap against the holiday's *real*
window (not the padded one, so this can't blur into a false "during");
"before"/"after" are the two ways a trip can miss the real window but
still land inside the ±2-day tolerance. Display text carries the
distinction ("During October half-term" / "Just before …" / "Just
after …"); the pill itself stays one consistent colour, since only the
wording was asked to change.

Verified two ways: real flag data re-run at the new wording (Hurghada's
post-Christmas trip → "Just after", Harare's Oct trip → "During", both
match a hand-check done before running); and a synthetic boundary sweep,
since the real data didn't happen to contain a "before" case to exercise
that branch directly rather than trust it by symmetry with "after". One
real off-by-one caught in *my own test*, not the code: a trip returning
exactly on the tolerance boundary correctly still counts (inclusive
`>=`), which my first hand-written expectation got wrong before checking
it against actual behaviour.

## places.py: two more metro-code overrides — CHI, ROM (2026-09-17)

Noticed by inspection of the rendered digest, not a report: `CHI`
(Chicago's metro code, spanning `ORD`+`MDW`) sat under "Other" instead of
resolving. Confirmed rather than assumed before fixing —
`airportsdata.load("IATA").get("CHI")` really does return `None`, while
`ORD` and `MDW` individually resolve fine, same shape as the three
existing overrides (`BUH`/`TCI`/`BAK`, §"digest rework" above). `ROM`
(Rome, spanning `FCO`+`CIA`) gets the same treatment — not currently
showing in the digest at the 30% bar, but the same class of gap, fixed
alongside rather than waiting for it to resurface and get re-diagnosed
from scratch. Country/continent strings for both
(`("Chicago","United States","North America")`,
`("Rome","Italy","Europe")`) match exactly what `pycountry_convert`
produces for a real airport in the same country — checked directly, not
retyped from memory.

Verified against real data: `CHI` now renders as "Chicago, United States"
under a new NORTH AMERICA section (the digest's first — everything so far
had been Africa/Europe/Asia), and the "OTHER" section — which `CHI` was
the only occupant of at the current bar — disappears entirely rather than
printing empty, confirming that section is genuinely data-driven and not
a static placeholder.

## Airline + flight number, end to end (2026-09-17)

Asked directly, with the reason stated up front: so a reader can go book
what they're looking at, not just recognise it's cheap. The data was
already there and unused — `airline`/`flight_number` are original
schema columns (`PLAN.md §4`), already load-bearing internally
(`_price_hash()` keys change detection on them), just never copied into a
flag record. `detect.py` now includes both when a flag is built.

**No pip package covers this the way `airportsdata` covers airports** —
checked before building anything, not assumed absent (see `scripts/
airlines.py`'s docstring for exactly what was searched). Vendored
`scripts/airlines_data.csv` instead, generated from OpenFlights' open
airline dataset, filtered to active + valid-2-letter-IATA rows. That
source has genuine duplicate-code entries even among active rows
(`VY` matches both Vueling Airlines and a much smaller Formosa Airlines).
Checked against one real night's swept data (`data/deltas/2026-09-17.
parquet`) rather than trusted blind:

- **91 distinct airline codes** in that one delta alone (destinations are
  worldwide, so this was always going to be a big list, not a handful of
  overrides like the `places.py` ones).
- **89 of 91 resolved** against the active+valid-IATA subset with no
  further work. `X1` has only a stale, inactive OpenFlights entry ("Nik
  Airways", marked "N") — left unresolved (falls back to the raw code)
  rather than shown on unreliable data, same convention `places.py`
  already uses for genuinely unknown codes. `RR` is the deliberate
  exclusion below.
- **A second real data-quality pass, caught by reading the generated file
  before committing it, not by any test:** the first cut of the CSV kept
  every row whose IATA field was exactly 2 characters — which let 10
  junk entries through (`&T`, `-+`, `--`, `..`, `;;`, a literal `\N`
  null-dump artifact, three Cyrillic-character codes) that happen to be
  2 characters but aren't real IATA codes at all. None were in the real
  91, so nothing user-visible broke, but they'd have shipped as silently
  wrong reference data. Fixed by validating `^[A-Z0-9]{2}$`, not just
  length — regenerated, re-confirmed all 91 real codes and the 4 hand-
  verified ones unchanged, junk count zero.
- **4 of the source's 19 duplicate-code groups actually appeared in real
  data** (`JL`, `LH`, `SQ`, `VY`) and were resolved by hand, checked
  against the actual route each flew (`VY` on `LHR`→`ALC` is obviously
  Vueling, a major European carrier on a plausible Spain route — not the
  small Taiwanese airline sharing that code in the source data). The
  other 15 duplicate groups never showed up in real data; they got a
  best-effort tiebreak (prefer a name without a Cargo/Domestic/Express/
  Regional qualifier, else the shorter name) that's recorded as
  unverified in the module docstring rather than presented with the same
  confidence as the 4 checked ones.
- **One code dropped outright by hand, not by any rule:** `RR`'s only
  "active" OpenFlights match was "REXAIR VIRTUEL" — a virtual/community
  entity, not a real commercial carrier a flight-search API would return.
  Spotted by reading the generated output and noticing one entry looked
  wrong, not by an automated check. Excluded; falls back to the raw code
  like `X1`.

Displayed as "Vueling Airlines VY1234" under the route/dates line, in
both the text and HTML renderers, built from one shared `_airline_label()`
so the two can't disagree — same pattern as `_holiday_phrase()`.

**A real gap, handled deliberately rather than hit by accident:** flags
already sitting in `data/flags/*.jsonl` were written by the old
`detect.py` and have no `airline`/`flight_number` keys at all. The 7-day
digest window mixes those with newly-flagged entries for about a week.
`_airline_label()` returns `None` for a flag missing the fields (checked
with a synthetic flag built to look like an old one, not just assumed
safe), and both renderers treat that exactly like "no holiday match" —
omit the line, never crash. Confirmed against real current data (today's
existing flags correctly show no airline line) and against a synthetic
flag with the fields present (confirmed the happy path renders in both
text and HTML, including alongside a holiday tag on the same fare card).

**Booking links are explicitly future work, not started** — recorded in
`TODO.md` along with what's already known from the abandoned
`grouped_prices` endpoint's `link` field, so that doesn't need
rediscovering later.

## min_trip_nights: a Saturday-to-Sunday exception (2026-09-17)

Prompted by the min_trip_nights=1 exploration earlier the same day, which
surfaced a real example (`LGW -> MAN`, Gatwick to Manchester — flagged as
likely noise at the time) alongside two plausible genuine 1-night trips.
Rather than lowering the floor for every 1-night trip, the ask was
narrower and more specific: let a real weekend getaway through, nothing
else under 2 nights.

`detect.is_eligible_trip_length(depart_date, return_date,
min_trip_nights)` is the one place this rule lives — the floor, plus a
named exception for a *literal* Saturday-to-Sunday 1-night trip, nothing
looser (not Friday-to-Sunday, not "starts on a weekend"). Both
`detect.py`'s flagging gate and `notify.py`'s digest-render filter
(`_flag_trip_eligible()`, converting a flag's ISO date strings before
delegating to the same function) call it, so the two can't independently
drift on what counts — same principle as `_prepare_digest()` and every
other shared-rule helper in this file.

**Real data answered its own open question:** all three fares the
min_trip_nights=1 exploration found turned out to be genuine
Saturday-to-Sunday trips, including the one flagged as probable noise
(`LGW -> MAN`). Under the literal rule as specified, a domestic weekend
break is exactly as eligible as an international one — a real
consequence, not a loophole, and worth knowing rather than silently
narrowing further on an assumption the user didn't ask for.

**Verified, and the limit of what verifying accomplished today, both
worth recording:** `is_eligible_trip_length()` checked directly against
five hand-built cases (Sat->Sun true; Sun->Mon, Fri->Sat, and same-day
all false; an ordinary 2-night midweek trip true) — confirms the rule is
exactly as narrow as specified, not "any weekend-adjacent" shape. Then
the same read-only simulation technique used earlier the same day
(`detect()`
re-run per real sweep date, `storage.save_index` patched to a no-op so
nothing touches disk) against real data. But: none of the three real
examples appear in tonight's actual digest, and grep against
`data/flags/*.jsonl` explains why precisely rather than leaving it a
mystery — `LGW -> MAN` has never been flagged at all (this rule is the
first time it could be), while the `TRN`/`TOS` instances *were* flagged
once, on 2026-09-05 to 09-09, before `min_trip_nights` existed as a rule
at all (added 2026-09-10) — both now outside the 7-day digest window
regardless. `detect()` only ever re-evaluates rows whose price changed on
a given delta night, by design (the whole point of delta-based storage);
it doesn't retroactively re-score prices that haven't moved. So this fix
is real and correct, but it's forward-looking only — none of today's
three examples will reappear in a real digest unless their price changes
again in a future sweep.

## digest_weekday: Sunday -> Friday (2026-09-17)

Reasoning stated directly: land with the weekend still ahead to decide,
not with it already over — a Sunday-morning digest gives a reader zero
days to act on it before Monday; a Friday-morning one gives two.

This breaks the original "`notify.digest_weekday` matches
`far_sweep_weekday`... one predictable weekly rhythm... rather than two
different cadences to track" reasoning from the 2026-09-01 delivery
section above — deliberately, not by oversight. That coupling was a
cognitive-load argument (one weekly day to remember, not two), not a
functional dependency: `far_sweep_weekday` only decides which night's
sweep also covers months 7-18, nothing about it reads or depends on
`digest_weekday`. Only the digest day was actually asked to move, so
`far_sweep_weekday` was left at Sunday rather than moved to match — noted
here in case the two cadences drifting apart matters later, rather than
silently reintroduced without a record of it.

## An alternate grouping: by trip length, not region (2026-09-18)

Asked for directly: "show me a format grouped by trip length" — built as
a genuine alternative to compare, not a replacement.
`build_digest_text_by_length()` / `build_digest_html_by_length()` exist
alongside the region-grouped versions; `run()` still calls the region
ones. Nothing wired into production changed.

Shares eligibility with the region view through a new `_eligible_fares()`
— the filter/collapse/place-resolve/holiday-check step both groupings
now call, split out specifically so a second grouping couldn't become a
second copy of that logic that could quietly drift from the first.
Bucket boundaries (`_LENGTH_BUCKETS`: Short 1-4, Medium 5-9, Long 10+)
were checked against a real week's actual distribution before being
picked, not guessed — 2,2,3,5,7,7,9,10,11,12,14,14,15,21 nights splits
3/4/7 across those three ranges, close enough to even to justify three
buckets over two or four.

**The real structural cost of the new axis, found by building it and
looking, not assumed away:** region-grouping nests every fare to the
same destination under one shared heading; trip-length grouping can't,
because the same destination's fares might belong in different buckets.
So every fare becomes its own full card, and the place name + continent
— previously inherited from a shared destination heading — move onto
each fare's own heading instead (continent shown as a second small muted
tag next to the code, not a new kind of element). The real week's data
happened not to contain a fare that splits across buckets, but it did
show the cost anyway: Strasbourg has two fares that land in the *same*
bucket, and where the region view would have shown one "Strasbourg,
France" card with two price rows nested in it, the length view shows two
separate "Strasbourg, France" cards. Less compact in that specific case —
a real trade-off of the new axis, not a defect, but worth knowing before
deciding whether to adopt it.

Verified: real current data end to end in both text and HTML: bucket
split matched the pre-check exactly (3/4/7); browser-checked at 375px,
not assumed fine from the text output — one continent name ("North
America") wraps to a second line on the fare-card heading at that width,
which reads fine but is worth knowing since nothing wrapped there in the
region-grouped view (continent was never inline with anything else).
