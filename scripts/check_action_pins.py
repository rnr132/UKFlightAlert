#!/usr/bin/env python3
"""
Flight Deal Scanner — GitHub Actions pin freshness check.

Every `uses:` in .github/workflows/ is pinned to a full commit SHA rather
than a moving tag (PLAN.md §4.2 / §5 — a compromised upstream tag can't
then silently roll into the nightly sweep). The price of pinning is that
security and runtime fixes don't arrive on their own: the SHA has to be
bumped by hand. This script is the standing reminder to do that.

It reads every pinned action out of the workflow files, asks the GitHub
API for each one's latest release, and — when the pinned SHA is behind —
opens (or refreshes) a single tracking issue carrying the exact
old -> new SHA and the `# vX.Y.Z` comment to write. Everything current is
a valid result: it prints that and exits zero without touching anything,
the same "an empty result is still a result" convention the rest of the
project uses.

stdlib only, on purpose. This is a maintenance job; it should not share
the data pipeline's dependency stack (pandas / pyarrow / ...) or be able
to break when one of those is bumped. urllib, not requests.

Wired to run quarterly from .github/workflows/check-action-pins.yml; also
runnable by hand at any time.

Usage:
    python scripts/check_action_pins.py            # CI mode: open/refresh the issue if anything is behind
    python scripts/check_action_pins.py --dry-run  # print the report, never touch an issue
"""
import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
API = "https://api.github.com"

ISSUE_TITLE = "Re-pin GitHub Actions to newer SHAs"
ISSUE_LABEL = "action-pins"

# uses: owner/repo@<40-hex sha>  [# vX.Y.Z]
# The version comment is optional so a pin added later without one is still
# tracked (reported as "?") rather than silently skipped. Sub-path actions
# like github/codeql-action/init@<sha> are released from owner/repo, so the
# slug is trimmed back to the first two segments before hitting the API.
PIN_RE = re.compile(
    r"uses:\s*(?P<slug>[\w.-]+/[\w.-]+(?:/[\w./-]+)?)@(?P<sha>[0-9a-f]{40})"
    r"(?:\s*#\s*(?P<ver>\S+))?"
)


def _workflow_files():
    if not WORKFLOWS_DIR.is_dir():
        raise RuntimeError(f"no workflows directory at {WORKFLOWS_DIR}")
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    if not files:
        raise RuntimeError(f"no workflow files under {WORKFLOWS_DIR}")
    return files


def find_pins():
    """Every SHA-pinned action across the workflow files, one entry per
    distinct (repo, sha) so a mismatched pin in one file is reported on its
    own rather than hidden behind an identically-named pin elsewhere."""
    pins = {}
    for wf in _workflow_files():
        for m in PIN_RE.finditer(wf.read_text()):
            repo = "/".join(m["slug"].split("/")[:2])
            key = (repo, m["sha"])
            entry = pins.setdefault(
                key, {"repo": repo, "sha": m["sha"], "ver": m["ver"] or "?", "files": set()}
            )
            entry["files"].add(wf.name)
    return sorted(pins.values(), key=lambda e: (e["repo"], e["sha"]))


def _api(path, token=None):
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "flightalert-check-action-pins",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _post(path, payload, token):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "flightalert-check-action-pins",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def latest_release(repo, token):
    """(tag, commit_sha) for the newest published, non-prerelease release of
    `repo`, or (None, None) if it publishes none. Annotated tags are
    dereferenced to the commit they point at so the comparison is
    commit-to-commit."""
    try:
        rel = _api(f"/repos/{repo}/releases/latest", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    tag = rel["tag_name"]
    obj = _api(f"/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}", token)["object"]
    if obj["type"] == "tag":
        obj = _api(f"/repos/{repo}/git/tags/{obj['sha']}", token)["object"]
    return tag, obj["sha"]


def check(token):
    rows = []
    for pin in find_pins():
        tag, sha = latest_release(pin["repo"], token)
        rows.append(
            {
                **pin,
                "latest_tag": tag,
                "latest_sha": sha,
                "behind": bool(sha) and sha != pin["sha"],
            }
        )
    return rows


def format_report(rows):
    """(markdown_body, any_behind). The body is written to be pasted as-is
    into the tracking issue."""
    behind = [r for r in rows if r["behind"]]
    if not behind:
        lines = ["All SHA-pinned actions are at their latest release.", ""]
        for r in rows:
            shown = r["latest_tag"] or "(no published releases)"
            lines.append(f"- `{r['repo']}` — pinned `{r['ver']}`, latest `{shown}`")
        return "\n".join(lines), False

    lines = [
        "One or more SHA-pinned actions are behind their latest release. "
        "Pins are deliberate (PLAN.md §4.2 / §5); this is the quarterly nudge "
        "to move them forward on purpose.",
        "",
    ]
    for r in behind:
        lines += [
            f"### `{r['repo']}` — `{r['ver']}` → `{r['latest_tag']}`",
            "",
            f"- In: {', '.join(sorted(r['files']))}",
            f"- Replace SHA `{r['sha']}`",
            f"- With    SHA `{r['latest_sha']}`",
            f"- Trailing comment → `# {r['latest_tag']}`",
            "",
        ]

    lines += [
        "Re-resolve each SHA independently before trusting the values above "
        "(same method they were first pinned by):",
        "",
        "```bash",
    ]
    for r in behind:
        lines.append(
            f"git ls-remote --tags --refs https://github.com/{r['repo']} "
            f"| grep -E 'refs/tags/{re.escape(r['latest_tag'] or '')}$'"
        )
    lines += [
        "```",
        "",
        "Then read that release's notes for any breaking change that touches a "
        "`schedule` + `workflow_dispatch`, checkout-then-commit workflow before "
        "bumping. Close this issue once the pins are updated — the quarterly "
        "check reopens a fresh one if anything falls behind again.",
    ]
    return "\n".join(lines), True


def _repo_slug():
    """owner/repo — from GITHUB_REPOSITORY under Actions, else the git remote."""
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    url = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"], text=True
    ).strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if not m:
        raise RuntimeError(f"can't parse owner/repo from git remote: {url!r}")
    return m.group(1)


def _ensure_label(slug, token):
    try:
        _post(
            f"/repos/{slug}/labels",
            {
                "name": ISSUE_LABEL,
                "color": "ededed",
                "description": "A SHA-pinned GitHub Action is behind its latest release",
            },
            token,
        )
    except urllib.error.HTTPError as e:
        if e.code != 422:  # 422 == already exists
            raise


def sync_issue(body, slug, token):
    """Open the tracking issue, or add a dated refresh comment to the open
    one that already exists. One issue, reused — not a fresh issue each
    quarter."""
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set — it's needed to open the tracking issue. "
            "Run with --dry-run to just print the report."
        )
    _ensure_label(slug, token)
    q = urllib.parse.quote(f"repo:{slug} is:issue is:open label:{ISSUE_LABEL}")
    hits = _api(f"/search/issues?q={q}", token)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if hits["total_count"]:
        num = hits["items"][0]["number"]
        _post(
            f"/repos/{slug}/issues/{num}/comments",
            {"body": f"Still behind as of {stamp}:\n\n{body}"},
            token,
        )
        return f"refreshed existing issue #{num}"
    created = _post(
        f"/repos/{slug}/issues",
        {"title": ISSUE_TITLE, "body": body, "labels": [ISSUE_LABEL]},
        token,
    )
    return f"opened issue #{created['number']}"


def run(dry_run=False):
    token = os.environ.get("GITHUB_TOKEN")
    rows = check(token)
    body, any_behind = format_report(rows)
    print(body)
    print()

    if not any_behind:
        print("check_action_pins: everything current — nothing to do")
        return {"behind": 0}
    if dry_run:
        print("check_action_pins: --dry-run, not opening an issue")
        return {"behind": sum(r["behind"] for r in rows), "dry_run": True}

    status = sync_issue(body, _repo_slug(), token)
    print(f"check_action_pins: {status}")
    return {"behind": sum(r["behind"] for r in rows), "issue": status}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report and exit; never create or comment on an issue.",
    )
    args = parser.parse_args()
    print(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
