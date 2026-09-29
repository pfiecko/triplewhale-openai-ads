#!/usr/bin/env python3
"""
OpenAI Ads conversion proxy (Cloud Function, gen2, public HTTPS).

The Shopify Custom Pixel (../shopify-pixel/pixel.js) posts every purchase here.
Per request the function:
  1. checks the X-Pixel-Secret header,
  2. re-applies the optional currency/country filter,
  3. builds the enhanced-conversions `user` object (email and external id hashed,
     geo raw, IP and User-Agent taken from the request), and
  4. forwards an order_created event to the OpenAI Ads Conversions API. The bearer
     token stays in Secret Manager and never reaches the browser.

event id is the Shopify order id, shared with the browser-side oaiq event, so OpenAI
deduplicates the pair.

Reference: https://developers.openai.com/ads/measurement-pixel
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

try:
    import runtime
except ImportError:  # running from a checkout: shared modules live in ../lib
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import runtime
import pixel_proxy as pp

try:
    import functions_framework
except ImportError:
    functions_framework = None

log = runtime.setup_logging("openai-ads-conversion-proxy")

CAPI_URL = "https://bzr.openai.com/v1/events"


def _respond(body: dict, status: int = 200):
    return runtime.json_response(body, status, pp.CORS_HEADERS)


def build_user(payload: dict, ip: str | None, user_agent: str | None) -> dict:
    """Identifiers are SHA-256 of the trimmed, lowercased value; geo, IP and UA are sent raw."""
    user: dict = {}
    email = pp.hash_identifier(pp.normalize_email(payload.get("email")))
    if email:
        user["email_sha256"] = email
    external = pp.hash_identifier(payload.get("external_id_sha256") or payload.get("external_id"))
    if external:
        user["external_id_sha256"] = external
    for key in ("country", "city", "zip_code"):
        if payload.get(key):
            user[key] = str(payload[key]).strip().lower()
    if ip:
        user["ip_address"] = ip
    if user_agent:
        user["user_agent"] = user_agent
    return user


def build_event(payload: dict, ip: str | None, user_agent: str | None) -> dict:
    event = {
        "id": str(payload["event_id"]),
        "type": "order_created",
        "timestamp_ms": int(time.time() * 1000),
        "source_url": payload.get("source_url") or "",
        "action_source": "web",
        "data": {
            "type": "contents",
            "amount": int(payload.get("amount_cents") or 0),  # integer minor units
            "currency": str(payload.get("currency") or "").upper(),
            "contents": payload.get("contents") or [],
        },
    }
    if payload.get("oppref"):
        event["oppref"] = payload["oppref"]
    user = build_user(payload, ip, user_agent)
    if user:
        event["user"] = user
    return event


def track(request):
    """POST from the Shopify Custom Pixel."""
    if request.method == "OPTIONS":
        return ("", 204, pp.CORS_HEADERS)
    if request.method != "POST":
        return _respond({"error": "method not allowed"}, 405)

    cfg = runtime.config()
    secrets, conv = cfg["gcp"]["secrets"], cfg["conversions"]
    if not pp.secret_matches(request.headers.get("X-Pixel-Secret"), runtime.secret(secrets["pixel_secret"])):
        log.warning("invalid X-Pixel-Secret from %s", request.remote_addr)
        return _respond({"error": "unauthorized"}, 401)

    payload = request.get_json(silent=True) or {}
    if not payload.get("event_id") or not payload.get("order_id"):
        return _respond({"error": "missing required fields: event_id, order_id"}, 400)
    if not pp.in_scope(payload, conv.get("allowed_currencies"), conv.get("allowed_countries")):
        log.info("out of scope: order_id=%s currency=%s", payload["order_id"], payload.get("currency"))
        return _respond({"ok": True, "skipped": "out_of_scope"})

    event = build_event(payload, pp.client_ip(request), request.headers.get("User-Agent"))
    try:
        resp = requests.post(f"{CAPI_URL}?pid={conv['pixel_id']}", timeout=30,
                             json={"validate_only": bool(conv.get("validate_only")), "events": [event]},
                             headers={"Authorization": f"Bearer {runtime.secret(secrets['capi_token'])}",
                                      "Content-Type": "application/json"})
    except requests.RequestException as exc:
        log.error("OpenAI CAPI request failed: %s", exc)
        return _respond({"ok": False, "error": str(exc)[:300]}, 502)

    # Surface rejections as 502 so they show up in request logs instead of hiding behind a 200.
    ok = 200 <= resp.status_code < 300
    (log.info if ok else log.error)("OpenAI CAPI %d id=%s amount=%d %s user=%s: %s", resp.status_code,
                                    event["id"], event["data"]["amount"], event["data"]["currency"],
                                    sorted(event.get("user", {})), resp.text[:300])
    return _respond({"ok": ok, "openai": {"status": resp.status_code, "body": resp.text[:300]}},
                    200 if ok else 502)


if functions_framework:
    track = functions_framework.http(track)
