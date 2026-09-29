import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from conftest import load_function

proxy = load_function("conversion-proxy")
PAYLOAD = {"event_id": "1001", "order_id": "1001", "amount_cents": 8950, "currency": "usd",
           "email": " Jane@Example.com ", "external_id": "gid://shopify/Customer/42",
           "country": "US", "city": "Austin", "zip_code": "78701", "oppref": "opp_1",
           "contents": [{"id": "SKU1", "quantity": 1}], "source_url": "https://shop.test/thank-you"}


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_ADS_PIXEL_SECRET", "px")
    monkeypatch.setenv("OPENAI_ADS_CAPI_TOKEN", "tok")


def _req(payload, secret="px"):
    return SimpleNamespace(method="POST", get_json=lambda silent=True: payload, remote_addr="9.9.9.9",
                           headers={"X-Pixel-Secret": secret, "X-Forwarded-For": "1.2.3.4", "User-Agent": "UA"})


def test_build_event_enhanced_conversions():
    ev = proxy.build_event(PAYLOAD, "1.2.3.4", "UA")
    assert ev["id"] == "1001" and ev["type"] == "order_created" and ev["oppref"] == "opp_1"
    assert ev["data"] == {"type": "contents", "amount": 8950, "currency": "USD",
                          "contents": [{"id": "SKU1", "quantity": 1}]}
    u = ev["user"]
    assert u["email_sha256"] == hashlib.sha256(b"jane@example.com").hexdigest()
    assert u["external_id_sha256"] == hashlib.sha256(b"gid://shopify/customer/42").hexdigest()
    assert (u["country"], u["city"], u["ip_address"], u["user_agent"]) == ("us", "austin", "1.2.3.4", "UA")


def test_prehashed_external_id_passes_through():
    h = "a" * 64
    assert proxy.build_user({"external_id_sha256": h}, None, None) == {"external_id_sha256": h}


def test_rejects_bad_secret():
    assert proxy.track(_req(PAYLOAD, secret="nope"))[1] == 401


def test_requires_ids():
    assert proxy.track(_req({"event_id": "1"}))[1] == 400


@pytest.mark.parametrize("oai_status,expected", [(200, 200), (400, 502)])
def test_reflects_openai_outcome(oai_status, expected):
    with mock.patch.object(proxy.requests, "post", return_value=mock.Mock(status_code=oai_status, text="{}")) as post:
        body, status, headers = proxy.track(_req(PAYLOAD))
    assert status == expected and headers["Access-Control-Allow-Origin"] == "*"
    assert post.call_args.args[0].endswith("?pid=PX_TEST")
    assert post.call_args.kwargs["json"]["validate_only"] is False
