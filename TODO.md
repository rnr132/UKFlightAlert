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

## Re-pin GitHub Actions to newer SHAs

`actions/checkout` and `actions/setup-python` are pinned to specific commit
SHAs in `.github/workflows/sweep.yml` (deliberately, per `PLAN.md §4.2`'s
fix). The first real run on 2026-08-28 printed a deprecation notice:

> Node.js 20 is deprecated. The following actions target Node.js 20 but are
> being forced to run on Node.js 24: `actions/checkout@11d5960a...`,
> `actions/setup-python@a26af69be...`

Not currently a failure — GitHub is transparently forcing both onto Node 24
— but worth bumping both pins to their newest releases sometime in the next
few months before that stops being true. Re-resolve the SHAs the same way
they were found originally (`git ls-remote --tags` against each repo) rather
than trusting a cached value here.
