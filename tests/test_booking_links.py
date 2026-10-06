"""booking_links.py: the one place the nightly run makes a network call that
isn't fetching prices. Two promises matter: a link problem can never fail
the sweep, and the account token never leaves the request header."""
from datetime import date

import pytest
import requests

import booking_links

CONFIG = {
    "api": {"base_url": "https://api.example.test", "token_header": "X-Access-Token"},
    "booking_links": {
        "trs": 111,
        "marker": 222,
        "create_link_path": "/links/v1/create",
        "sub_id": "digest",
        "shorten": True,
        "requests_per_minute": 100,
    },
}


def _flag(dest="TOS", depart="2026-10-10", ret="2026-10-11"):
    return {"origin_airport": "LGW", "destination": dest, "depart_date": depart, "return_date": ret}


# --- the URL itself --------------------------------------------------------------


def test_matches_the_worked_example_in_travelpayouts_docs():
    # LGW -> KTW, depart 11 Oct, return 14 Oct, 1 adult
    assert booking_links.build_search_url("LGW", "KTW", "2026-10-11", "2026-10-14") == (
        "https://www.aviasales.com/search/LGW1110KTW14101"
    )


def test_day_and_month_are_zero_padded_and_dates_may_be_date_objects():
    assert booking_links.build_search_url("STN", "ROM", date(2027, 1, 9), date(2027, 1, 11)) == (
        "https://www.aviasales.com/search/STN0901ROM11011"
    )


# --- enrichment never breaks the sweep --------------------------------------------


def test_every_flag_gets_a_plain_link_and_converted_ones_get_a_partner_link(monkeypatch):
    flags = [_flag("TOS"), _flag("CWL")]
    plain_tos = booking_links.build_search_url("LGW", "TOS", "2026-10-10", "2026-10-11")
    monkeypatch.setattr(booking_links, "create_partner_links", lambda urls, token, cfg: {plain_tos: "https://p.example/abc"})

    out = booking_links.attach_booking_links(flags, CONFIG, "tok")

    assert out[0]["search_url"] == plain_tos and out[0]["partner_url"] == "https://p.example/abc"
    assert out[1]["search_url"].endswith("CWL11101") or "CWL" in out[1]["search_url"]
    assert "partner_url" not in out[1], "a link the API rejected keeps just the plain one"


def test_a_conversion_failure_degrades_to_plain_links_instead_of_raising(monkeypatch, capsys):
    def boom(*a, **k):
        raise requests.ConnectionError("api down")

    monkeypatch.setattr(booking_links, "create_partner_links", boom)

    out = booking_links.attach_booking_links([_flag()], CONFIG, "tok")

    assert "search_url" in out[0] and "partner_url" not in out[0]
    assert "conversion failed" in capsys.readouterr().out


def test_no_flags_means_no_network_call(monkeypatch):
    monkeypatch.setattr(booking_links, "create_partner_links", lambda *a, **k: pytest.fail("network call with nothing to convert"))
    assert booking_links.attach_booking_links([], CONFIG, "tok") == []


# --- the API call ------------------------------------------------------------------


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class _FakeSession:
    calls = []
    script = []  # statuses to answer with, in order; empty = always 200 echoing every link as converted

    def post(self, url, headers=None, json=None, timeout=None):
        _FakeSession.calls.append({"url": url, "headers": headers, "json": json})
        if _FakeSession.script:
            status = _FakeSession.script.pop(0)
            if status != 200:
                return _Resp(status)
        links = [{"url": l["url"], "code": "success", "partner_url": "p:" + l["url"][-6:]} for l in json["links"]]
        return _Resp(200, {"result": {"links": links}})


@pytest.fixture
def fake_api(monkeypatch):
    _FakeSession.calls, _FakeSession.script = [], []
    monkeypatch.setattr(booking_links.requests, "Session", _FakeSession)
    monkeypatch.setattr(booking_links.time, "sleep", lambda s: None)
    return _FakeSession


def test_links_go_in_batches_of_ten_with_the_account_ids(fake_api):
    urls = [f"https://www.aviasales.com/search/LGW{i:04d}" for i in range(25)]

    out = booking_links.create_partner_links(urls, "secret-token", CONFIG)

    assert [len(c["json"]["links"]) for c in fake_api.calls] == [10, 10, 5]
    assert len(out) == 25
    first = fake_api.calls[0]["json"]
    assert (first["trs"], first["marker"], first["shorten"]) == (111, 222, True)
    assert first["links"][0]["sub_id"] == "digest"
    assert fake_api.calls[0]["url"] == "https://api.example.test/links/v1/create"


def test_the_token_travels_in_the_header_and_never_the_body(fake_api):
    booking_links.create_partner_links(["https://www.aviasales.com/search/X"], "secret-token", CONFIG)

    call = fake_api.calls[0]
    assert call["headers"] == {"X-Access-Token": "secret-token"}
    assert "secret-token" not in repr(call["json"]) and "secret-token" not in call["url"]


def test_a_transient_server_error_is_retried_then_succeeds(fake_api):
    fake_api.script = [503, 200]
    out = booking_links.create_partner_links(["https://www.aviasales.com/search/X"], "t", CONFIG)
    assert len(fake_api.calls) == 2 and len(out) == 1


def test_a_bad_token_is_not_retried(fake_api):
    fake_api.script = [401]
    with pytest.raises(requests.HTTPError):
        booking_links.create_partner_links(["https://www.aviasales.com/search/X"], "t", CONFIG)
    assert len(fake_api.calls) == 1, "retrying a rejected credential only burns calls"


def test_a_single_unconvertible_link_is_dropped_not_fatal(monkeypatch):
    class Mixed(_FakeSession):
        def post(self, url, headers=None, json=None, timeout=None):
            links = [
                {"url": json["links"][0]["url"], "code": "success", "partner_url": "p:ok"},
                {"url": json["links"][1]["url"], "code": "failed", "partner_url": ""},
            ]
            return _Resp(200, {"result": {"links": links}})

    monkeypatch.setattr(booking_links.requests, "Session", Mixed)
    monkeypatch.setattr(booking_links.time, "sleep", lambda s: None)

    out = booking_links.create_partner_links(["https://a.example/1", "https://a.example/2"], "t", CONFIG)

    assert out == {"https://a.example/1": "p:ok"}
