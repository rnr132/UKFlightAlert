"""The weekly owner report: what it says (so a quiet week is explainable),
and the rules for sending it (owner's inbox only, on its day, once, and
never able to break the digest)."""
import email
import email.header
import json
import smtplib
from datetime import date

import pytest

import detect
import notify
from helpers import CFG, FakeSMTP, sent_messages

MONDAY = date(2026, 10, 5)

OWNER_CFG = {**CFG, "notify": {**CFG["notify"], "owner_report_weekday": 0}}


def _sweep(day, *, failed=0, weekend=None, holiday=None):
    rec = {"sweep_date": day, "calls_attempted": 70, "calls_failed": failed}
    if weekend is not None:
        rec["funnel"] = {"weekend": weekend, "holiday": holiday}
    return rec


def _funnel(changed, shape, lead, seen, hist, low, drop, flagged):
    return dict(
        zip(
            ["changed", "right_shape", "within_lead_cap", "seen_enough", "has_history", "new_low", "drop_ok", "flagged"],
            [changed, shape, lead, seen, hist, low, drop, flagged],
        )
    )


def test_report_sums_the_week_and_names_the_tightest_tunable_gate():
    sweeps = [
        _sweep("2026-10-04", weekend=_funnel(1000, 200, 50, 10, 10, 2, 1, 1), holiday=_funnel(1000, 600, 100, 40, 40, 8, 2, 0)),
        _sweep("2026-10-05", weekend=_funnel(1000, 100, 30, 10, 10, 4, 1, 0), holiday=_funnel(1000, 400, 100, 40, 40, 6, 0, 0)),
    ]

    report = notify.build_owner_report(MONDAY, sweeps=sweeps, sends=[])

    assert "2 night(s) ran, 0 failed API call(s)" in report
    assert "2,000" in report  # fares that changed, summed over both sweeps
    # weekend: seen/lead = 20/80 = 25% is the lowest ratio of the tunable gates
    assert 'Tightest tunable gate for weekend: "seen on enough nights" kept 25%' in report
    assert "detection.min_observations" in report
    assert "scripts/replay.py" in report


def test_report_counts_the_digests_that_went_out():
    sends = [
        {"as_of": "2026-10-02", "sent": True, "weekend_count": 0, "holiday_count": 1, "recipients_count": 3, "failed_count": 0},
        {"as_of": "2026-10-03", "sent": False, "reason": "no_flags"},
        {"as_of": "2026-10-04", "sent": False, "reason": "no_flags"},
    ]
    sweeps = [_sweep(f"2026-10-0{d}") for d in (2, 3, 4)]

    report = notify.build_owner_report(date(2026, 10, 4), sweeps=sweeps, sends=sends)

    assert "sent on 1 night(s) to 3 recipient(s) — 0 weekend and 1 holiday fare(s), 0 failed send(s)" in report
    assert "Quiet nights: 2 of 3 had nothing new to send" in report


def test_a_week_with_no_sweeps_says_so_loudly():
    report = notify.build_owner_report(MONDAY, sweeps=[], sends=[])
    assert "NO SWEEPS RECORDED THIS WEEK" in report


def test_missing_funnel_is_explained_not_faked():
    report = notify.build_owner_report(MONDAY, sweeps=[_sweep("2026-10-05")], sends=[])
    assert "No funnel recorded yet" in report
    assert "Tightest" not in report


def test_partial_funnel_week_is_flagged_as_partial():
    sweeps = [
        _sweep("2026-10-04"),
        _sweep("2026-10-05", weekend=_funnel(10, 5, 4, 3, 3, 2, 1, 1), holiday=_funnel(10, 5, 4, 3, 3, 2, 1, 1)),
    ]
    assert "funnel recorded for 1 of 2 sweeps" in notify.build_owner_report(MONDAY, sweeps=sweeps, sends=[])


def test_old_sweeps_outside_the_week_are_ignored():
    sweeps = [_sweep("2026-09-01", failed=9), _sweep("2026-10-05")]
    report = notify.build_owner_report(MONDAY, sweeps=sweeps, sends=[])
    assert "1 night(s) ran, 0 failed" in report


def test_the_report_never_contains_an_address():
    sends = [{"as_of": "2026-10-05", "sent": True, "weekend_count": 1, "holiday_count": 0, "recipients_count": 3}]
    assert "@" not in notify.build_owner_report(MONDAY, sweeps=[_sweep("2026-10-05")], sends=sends)


# --- sending rules -------------------------------------------------------------


@pytest.fixture
def mail(tmp_path, monkeypatch):
    FakeSMTP.reset()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_PASSWORD", "test-key")
    monkeypatch.setattr(notify, "NOTIFY_HEARTBEAT_PATH", tmp_path / "notify_heartbeat.jsonl")
    monkeypatch.setattr(notify, "SWEEP_HEARTBEAT_PATH", tmp_path / "heartbeat.jsonl")
    (tmp_path / "heartbeat.jsonl").write_text(json.dumps(_sweep("2026-10-05")) + "\n")
    return tmp_path


def test_sent_on_its_weekday_to_the_owner_only(mail):
    assert notify.send_owner_report_if_due(OWNER_CFG, MONDAY, "owner@example.test") is True

    (envelope_from, envelope_to, raw), = sent_messages()
    assert envelope_to == ["owner@example.test"]
    parsed = email.message_from_string(raw)
    assert "owner report" in str(email.header.make_header(email.header.decode_header(parsed["Subject"])))
    assert "HEALTH" in parsed.get_payload(decode=True).decode("utf-8")
    assert parsed["List-Unsubscribe"] is None, "the owner doesn't need an unsubscribe link to themselves"


def test_not_sent_on_any_other_weekday(mail):
    assert notify.send_owner_report_if_due(OWNER_CFG, date(2026, 10, 6), "owner@example.test") is False
    assert sent_messages() == []


def test_needs_a_reply_to_inbox_to_send_to(mail, capsys):
    assert notify.send_owner_report_if_due(OWNER_CFG, MONDAY, None) is False
    assert sent_messages() == []
    assert "isn't set" in capsys.readouterr().out


def test_only_once_per_day_even_if_the_workflow_reruns(mail):
    assert notify.send_owner_report_if_due(OWNER_CFG, MONDAY, "owner@example.test") is True
    assert notify.send_owner_report_if_due(OWNER_CFG, MONDAY, "owner@example.test") is False
    assert len(sent_messages()) == 1


def test_a_failure_to_send_never_raises(mail, monkeypatch, capsys):
    FakeSMTP.drop_after = 0

    assert notify.send_owner_report_if_due(OWNER_CFG, MONDAY, "owner@example.test") is False

    assert "NOT sent" in capsys.readouterr().out
    assert not notify._owner_report_already_sent(MONDAY), "a failed send must not count as sent"


def test_a_bug_in_building_the_report_still_cannot_break_the_digest(mail, monkeypatch, capsys):
    monkeypatch.setattr(notify, "build_owner_report", lambda *a, **k: 1 / 0)

    assert notify.send_owner_report_if_due(OWNER_CFG, MONDAY, "owner@example.test") is False
    assert "ZeroDivisionError" in capsys.readouterr().out


def test_run_sends_the_report_even_on_a_night_the_digest_has_nothing(mail, monkeypatch):
    """The 'finally' in run(): quiet and failing nights are when the
    owner most wants the report, so it can't hang off the happy path."""
    monkeypatch.setattr(detect, "FLAGS_DIR", mail / "flags")  # no flags at all
    monkeypatch.setattr(notify, "PREVIEW_PATH", mail / "p.html")
    monkeypatch.setenv("REPLY_TO", "owner@example.test")
    monkeypatch.setenv("NOTIFY_RECIPIENTS", "a@example.org")

    result = notify.run(config=OWNER_CFG, as_of=MONDAY)

    assert result["reason"] == "no_flags"
    assert [to for _, to, _ in sent_messages()] == [["owner@example.test"]]


def test_run_sends_the_report_even_when_the_digest_step_itself_fails(mail, monkeypatch):
    """A failed night is the night the owner most needs the report."""
    flags_dir = mail / "flags" / "weekend"
    flags_dir.mkdir(parents=True)
    flag = {
        "product": "weekend", "flagged_at": MONDAY.isoformat(), "origin_airport": "LGW", "destination": "TOS",
        "depart_date": "2026-10-10", "return_date": "2026-10-11", "trip_type": "round_trip", "price_gbp": 95.0,
        "prior_min_gbp": 96.0, "prior_median_gbp": 200.0, "drop_pct_vs_median": 0.5, "observation_count": 9,
        "airline": "DY", "flight_number": "1303",
    }
    (flags_dir / f"{MONDAY.isoformat()}.jsonl").write_text(json.dumps(flag) + "\n")
    monkeypatch.setattr(detect, "FLAGS_DIR", mail / "flags")
    monkeypatch.setattr(notify, "PREVIEW_PATH", mail / "p.html")
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    monkeypatch.setenv("REPLY_TO", "owner@example.test")
    monkeypatch.setenv("NOTIFY_RECIPIENTS", "a@example.org")
    FakeSMTP.refuse = {"a@example.org"}  # the only recipient bounces -> the digest step raises

    with pytest.raises(RuntimeError):
        notify.run(config=OWNER_CFG, as_of=MONDAY)

    assert [to for _, to, _ in sent_messages()] == [["owner@example.test"]]
