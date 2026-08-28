# PATTERNS.md

Conventions and rough edges in the automation layer of
`rnr132/RN-credit-news-daily-digest`. Written against the repo as of
2026-08-27.

> **Status:** this describes the workflows *after* the three replacement files
> (`daily-digest.yml`, `article-ideas.yml`, `requirements.txt`) are pasted in.
> Until then §1–3 describe the intended state, not the live one. §6 lists what
> changed and why.

---

## 1. Workflow structure

Two scheduled workflows in `.github/workflows/`:

| File | Name | Schedule (UTC) | Script |
|---|---|---|---|
| `daily-digest.yml` | Daily Watchlist Digest | `29 7 * * *` — daily 07:29 | `scripts/generate_digest.py` |
| `article-ideas.yml` | Watchlist Article Ideas | `17 9 * * 1,3,5` — Mon/Wed/Fri 09:17 | `scripts/watchlist_article_ideas.py` |

Both share the same skeleton: checkout → setup-python → pip install → run
script → commit and push. Both carry `workflow_dispatch: {}` alongside the
schedule.

**Patterns worth keeping:**

- **`workflow_dispatch` on both.** GitHub's scheduled runs are best-effort;
  the manual trigger means a missed morning can be replayed by hand, and it's
  the API surface an external scheduler would call if that fallback is ever
  needed.
- **Independent workflows, no shared job.** A failure in the ideas run can't
  block the digest.
- **All logic in `scripts/`, YAML only orchestrates.** YAML is the one thing
  that can't be tested without pushing, so it should stay thin.
- **Off-the-hour cron.** 07:29 and 09:17 rather than round hours. GitHub's
  scheduler is most congested at `:00`, and jobs queued there are the ones
  most likely to be delayed or dropped.
- **Config-driven with in-code defaults.** The model is read as
  `config.get("model", "claude-haiku-4-5-20251001")` and search budget as
  `config.get("max_web_searches", 8)`, so a run still succeeds if
  `config.json` loses a key.
- **One dependency source.** Both workflows install from `requirements.txt`.
- **One Python version.** Both pin 3.12.

**Reliability, measured:** `digest_history.json` holds 55 entries from
2026-07-04 to 2026-08-26 with exactly one gap (2026-07-05). The ideas files
land cleanly on Mon/Wed/Fri with no misses. Scheduling has been far more
reliable than the external-cron fallback plan assumed — that plan can stay
shelved.

---

## 2. Secrets handling

Two secrets, both injected at **step level**, not job or workflow level:

- `ANTHROPIC_API_KEY` — both workflows.
- `EMAIL_APP_PASSWORD` — daily digest only, for the optional IMAP fetch that
  feeds email content into the digest. The script degrades gracefully: if the
  password or username is missing it prints a skip notice and continues rather
  than failing the run.

Both are read via `os.environ.get`, never passed as CLI arguments — that keeps
them out of process listings and out of the log if `set -x` is ever switched
on. `ANTHROPIC_API_KEY` raises a clear `RuntimeError` when absent instead of
failing deep inside an HTTP call.

**The repo is public**, a hard constraint (free Pages) that shapes the rest.
Actions masks the exact secret string but not derived or partial values, and
Actions logs on a public repo are world-readable and scraped.

One thing to stay aware of: on a JSON parse failure the script does
`print(text[:2000])`, dumping 2000 characters of raw model output into a public
log. Right instinct for debugging, no secret in it, but permanent and public.
Worth remembering before that dump is ever widened.

---

## 3. The commit-data-back step

The load-bearing pattern. There's no server, so `digest_history.json` *is* the
database, and the only way a scheduled run persists anything is by committing
to the repo it just checked out.

Both workflows now use the same block:

```yaml
git config user.name "watchlist-bot"
git config user.email "watchlist-bot@users.noreply.github.com"
git add <paths>
if git diff --staged --quiet; then
  echo "No changes to commit"
else
  git commit -m "<label>: $(date -u +'%Y-%m-%d')"
  git pull --rebase --autostash
  git push
fi
```

Digest stages `index.html archive.html digest_history.json`; ideas stages
`article_ideas`.

**Why each piece is there:**

- **The empty-tree guard.** Since the minimum item-count floor was removed, a
  genuinely quiet day can produce zero new items. Without the guard
  `git commit` exits non-zero on an empty tree and reddens the run, making a
  correct no-op look like a broken pipeline. A quiet day must exit zero.
- **`git pull --rebase --autostash` before push.** On Mon/Wed/Fri both
  workflows push to the same branch, now 108 minutes apart. The digest is a
  tool-use loop with up to 8 web searches, so a slow morning eats that margin.
  Without the rebase, an overlap means the second push is rejected as
  non-fast-forward and the run fails *after* the API spend is already gone.
- **`timeout-minutes: 20` on the digest.** A hung API call would otherwise sit
  there consuming Actions minutes until the six-hour default.
- **A single bot identity.** `watchlist-bot` on both, so `git log --author` is
  meaningful and automated commits stay off contribution graphs.
- **UTC-stamped commit messages.** Correlate a bad digest with a run without
  opening the Actions tab.

---

## 4. Known limitations

**4.1 — Cron is always UTC, and does not observe British Summer Time.** The
effective London time shifts by an hour twice a year. If the intent is "before
the UK open," that anchor drifts. Currently unhandled and probably fine, but
it's a choice rather than a default.

**4.2 — Unpinned actions.** `actions/checkout@v4` and
`actions/setup-python@v5` follow moving tags. Reasonable trade at this scale;
worth knowing it's a trade.

**4.3 — No failure notification.** If the API call fails, the JSON truncates,
or a push is rejected, the run goes red in a tab nobody is watching. The
07-05 gap presumably looked exactly like this. Actions can email on failure but
the setting is per-account — confirm it's on.

**4.4 — Silent gaps.** A skipped run leaves a hole in `digest_history.json`
that only surfaces when the Coverage Status panel goes amber. Cheap fix: have
`generate_digest.py` check the newest entry on startup and log a warning if the
gap exceeds one day. Turns a silent failure into a visible one. *Not yet done.*

**4.5 — Dedup logs the decision but not the reason.** Line 412 prints the
dropped title, which is the important half, but not the similarity score or the
prior story it matched. When it eventually drops something it shouldn't — at a
5/6 catch rate it will — the false positive can't be diagnosed after the fact.
Adding score and matched title makes the rate measurable rather than guessed
at. *Not yet done.*

**4.6 — 2026-08-27 digest missing.** At clone time the newest entry was
08-26 and the newest commit was the 08-26 ideas run, with the 07:29 slot long
past. Could be a delayed run, a failure, or clone timing. Worth a glance at the
Actions tab.

---

## 5. Corrections to the first draft of this file

Written before reading the repo; four claims were wrong:

- **"Cron comments say 08:00 but run at 09:00."** Not true of these files. The
  digest had no comment and ran at 07:29; the ideas workflow's per-line
  comments matched their expressions. The genuinely stale text was the
  `name: Weekly Article Ideas` and a "Runs every Monday" header sitting above
  three cron lines — both now fixed.
- **"The dedup safety net has no observability."** Wrong. It logs on both
  paths, including the dropped title.
- **"Scheduled runs are unreliable here."** Overstated. One miss in 55 days.
- **"Actions may be unpinned."** Hedged as inferred; it's confirmed.

---

## 6. Changelog — 2026-08-27

| Change | Reason |
|---|---|
| Pin `anthropic==1.1.0` in `requirements.txt` | Was installed unpinned. The SDK has crossed 0.x → 1.x, the version boundary where breaking changes are permitted. The script survived only because it uses `client.messages.create`, the most stable call. |
| Ideas workflow installs from `requirements.txt` | It ran its own `pip install anthropic`, so dependencies lived in two places and only one was version-controlled. |
| Ideas workflow 3.11 → 3.12 | Both scripts read the same data file; version drift means a bug can reproduce in one workflow and not the other. |
| `git pull --rebase --autostash` added to both | Prevents non-fast-forward rejection when the two Mon/Wed/Fri runs overlap. |
| `timeout-minutes: 20` on digest | Caps a hung tool-use loop. |
| Commit guard rewritten as `if/else` | `check && echo \|\| commit` behaved correctly but read as a riddle. |
| Ideas run 09:00 → 09:17 | Avoids top-of-hour scheduler congestion; also widens the gap from the digest. |
| Unified bot identity to `watchlist-bot` | Was two identities across the two workflows. |
| Renamed "Weekly Article Ideas" → "Watchlist Article Ideas"; removed stale Monday comment | Cadence has been Mon/Wed/Fri for some time. |

**After applying:** watch the next scheduled run. Changing a cron expression
re-registers the schedule with GitHub, which occasionally skips one cycle
before settling. A single miss immediately after this change is expected, not
a regression.

**Not done, deliberately** — these touch `generate_digest.py` rather than the
plumbing: the startup gap warning (4.4) and dedup score logging (4.5).

---

## 7. Invariants

- All persistence flows through `digest_history.json`, committed back by the
  run that produced it.
- All interactivity is client-side; the workflow's only output is static files.
- Secrets are step-scoped, env-injected, never argv, in a public repo.
- An empty result is a valid result and must exit zero.
- Prompt-level fixes are primary; Python-side dedup is a backstop.
- Optional inputs (email) degrade to a skip, never to a failure.
- Both workflows stay structurally identical — same Python, same dependency
  source, same commit block.
