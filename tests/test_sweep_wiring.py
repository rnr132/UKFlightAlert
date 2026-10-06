"""sweep.py's real_sweep(): the glue between fetching, detection, link
enrichment and the heartbeat. The network is stubbed and storage points at
a tmp dir, but the function body that runs nightly runs for real — this is
the one path where a typo wouldn't show up until the scheduled job broke."""
import json
from datetime import datetime, timedelta, timezone

import booking_links
import detect
import sweep
from config import load_config
from conftest import DETECT_CFG


def _next_saturday_at_least_two_days_out(today):
    d = today + timedelta(days=2)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d


def test_real_sweep_writes_per_product_flags_and_records_the_funnel(store, tmp_path, monkeypatch):
    today = datetime.now(timezone.utc).date()
    sat = _next_saturday_at_least_two_days_out(today)
    store.seed(today, sat, sat + timedelta(days=1), [200.0] * 6, 100.0, dest="AAA")

    config = load_config()
    config["detection"] = dict(DETECT_CFG["detection"])
    monkeypatch.setattr(sweep, "HEARTBEAT_PATH", tmp_path / "heartbeat.jsonl")
    monkeypatch.setattr(sweep, "load_token", lambda cfg: "dummy-token")
    monkeypatch.setattr(
        sweep,
        "sweep_one",
        lambda *a, **k: {"origin": a[6], "month": a[7], "ok": True, "fetched": 0, "changed": 0, "cheapest": None},
    )
    enriched = []
    monkeypatch.setattr(
        booking_links, "attach_booking_links", lambda flags, cfg, token: enriched.append(len(flags)) or flags
    )

    sweep.real_sweep(config, origin_filter="LHR")

    heartbeat = json.loads((tmp_path / "heartbeat.jsonl").read_text().splitlines()[-1])
    assert heartbeat["flags_found"] == 1
    assert heartbeat["flags_found_by_product"]["weekend"] == 1
    funnel = heartbeat["funnel"]["weekend"]
    assert funnel["changed"] == 1 and funnel["flagged"] == 1
    assert list(funnel) == list(detect.FUNNEL_STAGES)

    written = detect.FLAGS_DIR / "weekend" / f"{today.isoformat()}.jsonl"
    assert [json.loads(line)["destination"] for line in written.read_text().splitlines()] == ["AAA"]
    assert not (detect.FLAGS_DIR / "holiday").exists(), "a quiet product must not create an empty file"
    assert sorted(enriched) == [0, 1], "link enrichment runs once per product, including the empty one"
