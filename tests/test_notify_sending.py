"""notify.py's sending path: one message per recipient, no address ever
shown to another recipient or printed to the (public) Actions log, Reply-To
and unsubscribe only when configured, and a re-run the same day not
re-emailing fares that already went out.

Everything here is hermetic: SMTP is faked, and every path notify.py writes
to is pointed at a tmp dir.
"""
import email
import json
import smtplib
from datetime import date

import pytest

import detect
import notify
from helpers import CFG, FakeSMTP, sent_messages

AS_OF = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.reset()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    monkeypatch.setenv("SMTP_PASSWORD", "test-key")
    monkeypatch.delenv("REPLY_TO", raising=False)
    monkeypatch.delenv("NOTIFY_RECIPIENTS", raising=False)


# --- one message per recipient -------------------------------------------------


def test_each_recipient_gets_their_own_message_and_never_sees_the_others():
    people = ["alice@example.org", "bob@example.net", "carol@example.com"]
    result = notify.send_email(CFG, "subj", "text body", "<p>html</p>", people)

    assert result == {"sent": 3, "failed": 0, "failures": {}}
    msgs = sent_messages()
    assert len(msgs) == 3
    for (envelope_from, envelope_to, raw), me in zip(msgs, people):
        assert envelope_from == "deals@example.test"
        assert envelope_to == [me]
        parsed = email.message_from_string(raw)
        assert parsed["To"] == me
        for other in people:
            if other != me:
                assert other not in raw, f"{other} leaked into {me}'s message"


def test_whole_run_uses_one_smtp_session():
    notify.send_email(CFG, "s", "t", "<p>h</p>", ["a@example.org", "b@example.org"])
    assert len(FakeSMTP.instances) == 1


# --- Reply-To / unsubscribe ----------------------------------------------------


def test_reply_to_and_list_unsubscribe_present_only_when_configured():
    notify.send_email(CFG, "s", "t", "<p>h</p>", ["a@example.org"], reply_to="owner@example.test")
    configured = email.message_from_string(sent_messages()[0][2])
    assert configured["Reply-To"] == "owner@example.test"
    assert configured["List-Unsubscribe"] == "<mailto:owner@example.test?subject=Unsubscribe>"

    FakeSMTP.instances = []
    notify.send_email(CFG, "s", "t", "<p>h</p>", ["a@example.org"])
    bare = email.message_from_string(sent_messages()[0][2])
    assert bare["Reply-To"] is None
    assert bare["List-Unsubscribe"] is None


def test_unsubscribe_line_only_rendered_when_it_can_be_honoured():
    flags = [_flag("weekend")]
    with_line, _, _ = notify.build_digest_text(flags, [], AS_OF, 0.2, can_unsubscribe=True)
    without, _, _ = notify.build_digest_text(flags, [], AS_OF, 0.2, can_unsubscribe=False)
    assert notify._UNSUBSCRIBE_LINE in with_line
    assert notify._UNSUBSCRIBE_LINE not in without

    html_with, _, _ = notify.build_digest_html(flags, [], AS_OF, 0.2, can_unsubscribe=True)
    html_without, _, _ = notify.build_digest_html(flags, [], AS_OF, 0.2, can_unsubscribe=False)
    assert "say stop" in html_with
    assert "say stop" not in html_without


def test_reply_to_env_var_is_validated_and_not_echoed(monkeypatch, capsys):
    monkeypatch.setenv("REPLY_TO", "owner@example.test")
    assert notify._load_reply_to(CFG) == "owner@example.test"

    monkeypatch.setenv("REPLY_TO", "a@example.test\nBcc: victim@example.net")
    assert notify._load_reply_to(CFG) is None
    assert "victim" not in capsys.readouterr().out

    monkeypatch.setenv("REPLY_TO", "")
    assert notify._load_reply_to(CFG) is None


# --- failures never leak addresses ---------------------------------------------


def test_one_refused_recipient_does_not_block_the_rest_or_leak_their_address():
    FakeSMTP.refuse = {"bad@example.org"}
    people = ["alice@example.org", "bad@example.org", "carol@example.org"]
    result = notify.send_email(CFG, "s", "t", "<p>h</p>", people)

    assert result["sent"] == 2
    assert result["failed"] == 1
    assert "bad@example.org" not in json.dumps(result)
    assert "550" in json.dumps(result)
    delivered_to = [to[0] for _, to, _ in sent_messages()]
    assert delivered_to == ["alice@example.org", "carol@example.org"]


def test_lost_connection_counts_everyone_not_yet_attempted_as_failed():
    FakeSMTP.drop_after = 1  # first goes through, second dies
    people = ["a@example.org", "b@example.org", "c@example.org", "d@example.org"]
    result = notify.send_email(CFG, "s", "t", "<p>h</p>", people)
    assert result["sent"] == 1
    assert result["failed"] == 3


def test_missing_api_key_fails_before_any_connection(monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD")
    with pytest.raises(RuntimeError, match="SMTP_PASSWORD"):
        notify.send_email(CFG, "s", "t", "<p>h</p>", ["a@example.org"])
    assert FakeSMTP.instances == []


# --- recipient list hygiene ------------------------------------------------------


def test_recipient_list_drops_duplicates_and_malformed_entries_without_printing_them(monkeypatch, capsys):
    monkeypatch.setenv(
        "NOTIFY_RECIPIENTS",
        " Alice@Example.org , bob@example.net,alice@example.org,not-an-email,  ,carol@example.com;dave@example.com",
    )
    assert notify._load_recipients(CFG) == ["Alice@Example.org", "bob@example.net"]
    out = capsys.readouterr().out
    assert "skipped 2 malformed and 1 duplicate" in out
    assert "not-an-email" not in out and "@" not in out


# --- run(): heartbeat and the same-day re-run guard ------------------------------


def _flag(product, destination="TOS", price=95.0, **over):
    f = {
        "product": product,
        "flagged_at": AS_OF.isoformat(),
        "origin_airport": "LGW",
        "destination": destination,
        "depart_date": "2026-10-10",
        "return_date": "2026-10-11",
        "trip_type": "round_trip",
        "price_gbp": price,
        "prior_min_gbp": price + 1,
        "prior_median_gbp": price * 2,
        "drop_pct_vs_median": 0.5,
        "observation_count": 9,
        "airline": "DY",
        "flight_number": "1303",
        "search_url": "https://www.aviasales.com/search/LGW1010TOS11101",
    }
    f.update(over)
    return f


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    flags_dir = tmp_path / "flags"
    monkeypatch.setattr(detect, "FLAGS_DIR", flags_dir)
    monkeypatch.setattr(notify, "NOTIFY_HEARTBEAT_PATH", tmp_path / "notify_heartbeat.jsonl")
    monkeypatch.setattr(notify, "PREVIEW_PATH", tmp_path / "preview.html")
    monkeypatch.setenv("NOTIFY_RECIPIENTS", "alice@example.org,bob@example.net,carol@example.com")
    monkeypatch.setenv("REPLY_TO", "owner@example.test")

    def write_flags(product, flags, day=AS_OF):
        d = flags_dir / product
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{day.isoformat()}.jsonl", "w") as fh:
            for f in flags:
                fh.write(json.dumps(f) + "\n")

    def heartbeat():
        path = tmp_path / "notify_heartbeat.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return write_flags, heartbeat


def test_run_sends_one_message_each_and_records_what_went_out(workspace):
    write_flags, heartbeat = workspace
    write_flags("weekend", [_flag("weekend")])

    result = notify.run(config=CFG, as_of=AS_OF)

    assert result["sent"] is True
    assert len(sent_messages()) == 3
    rec = heartbeat()[-1]
    assert rec["sent"] is True
    assert rec["recipients_count"] == 3 and rec["sent_count"] == 3 and rec["failed_count"] == 0
    assert rec["sent_flag_ids"] == ["weekend|LGW|TOS|2026-10-10|2026-10-11|95.0"]
    assert "@" not in json.dumps(rec), "the committed heartbeat must never contain an address"


def test_same_day_rerun_does_not_email_everyone_twice(workspace):
    write_flags, heartbeat = workspace
    write_flags("weekend", [_flag("weekend")])
    notify.run(config=CFG, as_of=AS_OF)
    FakeSMTP.instances = []

    again = notify.run(config=CFG, as_of=AS_OF)

    assert again["sent"] is False and again["reason"] == "already_sent"
    assert sent_messages() == []
    assert heartbeat()[-1]["reason"] == "already_sent"


def test_same_day_rerun_still_sends_a_genuinely_new_fare(workspace):
    write_flags, _ = workspace
    write_flags("weekend", [_flag("weekend", destination="TOS")])
    notify.run(config=CFG, as_of=AS_OF)
    FakeSMTP.instances = []

    write_flags("weekend", [_flag("weekend", destination="TOS"), _flag("weekend", destination="CWL", price=120.0)])
    result = notify.run(config=CFG, as_of=AS_OF)

    assert result["sent"] is True
    body = _decoded_text(sent_messages()[0][2])
    assert "Cardiff" in body
    assert "Troms" not in body, "the fare that already went out must not be re-sent"


def test_same_fare_at_a_lower_price_counts_as_new(workspace):
    write_flags, _ = workspace
    write_flags("weekend", [_flag("weekend", price=95.0)])
    notify.run(config=CFG, as_of=AS_OF)
    FakeSMTP.instances = []

    write_flags("weekend", [_flag("weekend", price=95.0), _flag("weekend", price=80.0)])
    result = notify.run(config=CFG, as_of=AS_OF)

    assert result["sent"] is True


def test_legacy_heartbeat_without_ids_is_treated_as_everything_sent(workspace, tmp_path):
    write_flags, _ = workspace
    write_flags("weekend", [_flag("weekend")])
    (tmp_path / "notify_heartbeat.jsonl").write_text(
        json.dumps({"as_of": AS_OF.isoformat(), "sent": True, "run_at": "2026-10-06T09:00:00+00:00"}) + "\n"
    )

    result = notify.run(config=CFG, as_of=AS_OF)

    assert result["reason"] == "already_sent"
    assert sent_messages() == []


def test_resend_flag_overrides_the_guard(workspace):
    write_flags, _ = workspace
    write_flags("weekend", [_flag("weekend")])
    notify.run(config=CFG, as_of=AS_OF)
    FakeSMTP.instances = []

    assert notify.run(config=CFG, as_of=AS_OF, resend=True)["sent"] is True
    assert len(sent_messages()) == 3


def test_partial_failure_still_records_the_night_then_fails_the_step(workspace):
    write_flags, heartbeat = workspace
    write_flags("weekend", [_flag("weekend")])
    FakeSMTP.refuse = {"bob@example.net"}

    with pytest.raises(RuntimeError, match="1 of 3 sends failed") as err:
        notify.run(config=CFG, as_of=AS_OF)

    assert "bob@example.net" not in str(err.value)
    rec = heartbeat()[-1]
    assert rec["sent"] is True and rec["sent_count"] == 2 and rec["failed_count"] == 1


def test_every_send_failing_records_no_ids_so_a_retry_is_not_blocked(workspace):
    write_flags, heartbeat = workspace
    write_flags("weekend", [_flag("weekend")])
    FakeSMTP.refuse = {"alice@example.org", "bob@example.net", "carol@example.com"}

    with pytest.raises(RuntimeError):
        notify.run(config=CFG, as_of=AS_OF)

    rec = heartbeat()[-1]
    assert rec["sent"] is False and rec["reason"] == "all_sends_failed"
    assert "sent_flag_ids" not in rec

    FakeSMTP.refuse = set()
    FakeSMTP.instances = []
    assert notify.run(config=CFG, as_of=AS_OF)["sent"] is True


def test_dry_run_and_test_modes_write_no_heartbeat(workspace):
    write_flags, heartbeat = workspace
    write_flags("weekend", [_flag("weekend")])

    notify.run(config=CFG, as_of=AS_OF, dry_run=True)
    notify.run(config=CFG, as_of=AS_OF, test_address="me@example.org")

    assert heartbeat() == []
    assert [to for _, to, _ in sent_messages()] == [["me@example.org"]]


def test_missing_reply_to_still_sends_but_without_unsubscribe(workspace, monkeypatch, capsys):
    write_flags, _ = workspace
    write_flags("weekend", [_flag("weekend")])
    monkeypatch.delenv("REPLY_TO")

    assert notify.run(config=CFG, as_of=AS_OF)["sent"] is True

    assert "REPLY_TO is not set" in capsys.readouterr().out
    parsed = email.message_from_string(sent_messages()[0][2])
    assert parsed["Reply-To"] is None


def _decoded_text(raw):
    parsed = email.message_from_string(raw)
    for part in parsed.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode("utf-8")
    return ""
