#!/usr/bin/env python3
"""
OpenAI Ads → Triple Whale spend import (Cloud Function, gen2).

Walks the OpenAI Ads Advertiser API hierarchy (campaigns → ad groups → ads), reads
each ad's daily insights and upserts them to Triple Whale Data-In, so OpenAI Ads
spend shows up next to Meta and Google.

Hierarchy: campaign → ad group → ad maps to Triple Whale campaign → adset → ad.
Ad thumbnails: OpenAI's creative.image_url is a signed URL that expires within
about a day, so creatives are copied to a private GCS bucket and served by the
creative-host function under a stable URL (see ../creative-host).

HTTP (deploy with --no-allow-unauthenticated; Cloud Scheduler calls it with OIDC):
  GET|POST /?days=7                                trailing window (default from config)
  GET|POST /?since=YYYY-MM-DD&until=YYYY-MM-DD     explicit window, within the most recent
                                                   `insights_days` days the API returns
  &dry_run=1                                       collect records, do not send them

CLI:
  python spend-import/main.py --days 7 --dry-run   # live OpenAI, no Triple Whale push
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

try:
    import runtime
except ImportError:  # running from a checkout: shared modules live in ../lib
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import runtime
import tw_data_in as tw

try:
    import functions_framework
except ImportError:
    functions_framework = None

log = runtime.setup_logging("openai-ads-tw-import")

OAI_BASE = "https://api.ads.openai.com/v1"
PAGE = 500


# ---------------------------------------------------------------------------
# OpenAI Ads Advertiser API
# ---------------------------------------------------------------------------

class OpenAIAds:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def get(self, path: str, params=None):
        r = self.s.get(f"{OAI_BASE}{path}", params=params or {}, timeout=30)
        if not r.ok:
            raise RuntimeError(f"OpenAI Ads {r.status_code} {path}: {r.text[:300]}")
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def campaigns(self) -> list[dict]:
        """Every non-archived campaign (paginated)."""
        out, after = [], None
        while True:
            batch = self.get("/campaigns", {"limit": PAGE, "order": "asc", **({"after": after} if after else {})})
            out += [c for c in batch if c.get("status") != "archived"]
            if len(batch) < PAGE:
                return out
            after = batch[-1].get("id")

    def ad_groups(self, campaign_id: str) -> list[dict]:
        return self.get("/ad_groups", {"campaign_id": campaign_id, "limit": PAGE})

    def ads(self, ad_group_id: str) -> list[dict]:
        return self.get("/ads", {"ad_group_id": ad_group_id, "limit": PAGE})

    def ad(self, ad_id: str) -> dict:
        return self.get(f"/ads/{ad_id}")

    def daily_insights(self, ad_id: str, days: int) -> list[dict]:
        """The most recent `days` daily rows. spend and clicks must be requested
        explicitly with fields[]; readable_time is a plain YYYY-MM-DD string."""
        return self.get(f"/ads/{ad_id}/insights", [
            ("time_granularity", "daily"), ("limit", days),
            ("fields[]", "spend"), ("fields[]", "clicks"),
            ("fields[]", "impressions"), ("fields[]", "readable_time"),
        ])


def tw_status(oai_status: str | None) -> str:
    return "ACTIVE" if oai_status == "active" else "PAUSED"


def ad_details(api: OpenAIAds, ad: dict) -> tuple[str, str | None, str | None, str]:
    """(name, signed image url, file_id, status). Fetch the full ad when the list item is sparse."""
    creative = ad.get("creative") or {}
    name, image, file_id, status = (ad.get("name") or creative.get("title"), creative.get("image_url"),
                                    creative.get("file_id"), ad.get("status"))
    if not name or image is None or not file_id:
        try:
            full = api.ad(ad["id"])
            fc = full.get("creative") or {}
            name = name or full.get("name") or fc.get("title")
            image = image if image is not None else fc.get("image_url")
            file_id = file_id or fc.get("file_id")
            status = status or full.get("status")
        except Exception as exc:
            log.warning("could not fetch ad %s: %s", ad.get("id"), exc)
    return name or ad["id"], image, file_id, tw_status(status)


# ---------------------------------------------------------------------------
# Creative re-hosting
# ---------------------------------------------------------------------------

class CreativeStore:
    """Copies creatives into a private GCS bucket keyed by OpenAI file_id and returns the
    creative-host URL. file_ids are content-addressed, so a stored file never changes."""

    def __init__(self, cfg: dict | None):
        self.cfg = cfg or {}
        self.bucket = None
        self.seen: dict[str, str | None] = {}

    def url(self, file_id: str | None, signed_url: str | None) -> str | None:
        if not file_id or not self.cfg.get("bucket") or not self.cfg.get("host_base"):
            return None
        if file_id not in self.seen:
            self.seen[file_id] = self._store(file_id, signed_url)
        return self.seen[file_id]

    def _store(self, file_id: str, signed_url: str | None) -> str | None:
        try:
            if self.bucket is None:
                from google.cloud import storage
                self.bucket = storage.Client().bucket(self.cfg["bucket"])
            blob = self.bucket.blob(file_id)
            if not blob.exists():
                if not signed_url:
                    return None
                r = requests.get(signed_url, timeout=30)
                r.raise_for_status()
                blob.upload_from_string(r.content, content_type=r.headers.get("Content-Type", "image/png").split(";")[0])
                log.info("re-hosted creative %s (%d bytes)", file_id, len(r.content))
            return f"{self.cfg['host_base'].rstrip('/')}/{file_id}"
        except Exception as exc:
            log.warning("could not re-host creative %s: %s", file_id, exc)
            return None


# ---------------------------------------------------------------------------
# Collect + map
# ---------------------------------------------------------------------------

def collect_rows(api: OpenAIAds, since: str, until: str, insights_days: int,
                 creatives: CreativeStore) -> list[dict]:
    """Flatten campaign → ad group → ad → daily insight rows inside [since, until]."""
    rows = []
    for c in api.campaigns():
        try:
            groups = api.ad_groups(c["id"])
        except Exception as exc:
            log.warning("ad groups for campaign %s: %s", c["id"], exc)
            continue
        for g in groups:
            try:
                ads = api.ads(g["id"])
            except Exception as exc:
                log.warning("ads for ad group %s: %s", g["id"], exc)
                continue
            for ad in ads:
                try:
                    insights = [i for i in api.daily_insights(ad["id"], insights_days)
                                if since <= (i.get("readable_time") or "") <= until]
                except Exception as exc:
                    log.warning("insights for ad %s: %s", ad["id"], exc)
                    continue
                if not insights:
                    continue
                name, signed, file_id, status = ad_details(api, ad)
                image = creatives.url(file_id, signed)
                for i in insights:
                    rows.append({
                        "date": i["readable_time"],
                        "campaign": {"id": c["id"], "name": c.get("name"), "status": tw_status(c.get("status"))},
                        "adset": {"id": g["id"], "name": g.get("name"), "status": tw_status(g.get("status"))},
                        "ad": {"id": ad["id"], "name": name, "status": status, "ad_image_url": image},
                        "spend": float(i.get("spend") or 0),
                        "clicks": int(i.get("clicks") or 0),
                        "impressions": int(i.get("impressions") or 0),
                    })
    return rows


def to_records(rows: list[dict], since: str, until: str, cfg: dict) -> list[dict]:
    t = cfg["triplewhale"]
    records = [
        tw.ad_record(shop=t["shop_id"], channel=t["channel"], channel_account_id=t["channel_account_id"],
                     event_date=r["date"], currency=t["currency"], campaign=r["campaign"],
                     adset=r["adset"], ad=r["ad"],
                     metrics={"spend": r["spend"], "clicks": r["clicks"], "impressions": r["impressions"]})
        for r in rows
    ]
    return tw.zero_fill(records, since, until)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def import_window(since: str, until: str, dry_run: bool) -> dict:
    cfg = runtime.config()
    secrets = cfg["gcp"]["secrets"]
    api = OpenAIAds(runtime.secret(secrets["openai_ads_read_token"]))
    rows = collect_rows(api, since, until, cfg["import"]["insights_days"],
                        CreativeStore(cfg.get("creatives")))
    records = to_records(rows, since, until, cfg)
    out = {"window": [since, until], "source_rows": len(rows), **tw.summarize(records),
           "dry_run": dry_run}
    if dry_run or not records:
        out["sample"] = records[:3]
        return out
    out["result"] = tw.send_records(records, runtime.secret(secrets["triplewhale_api_key"]))
    return out


def spend_import(request):
    """HTTP entry point."""
    args = request.args
    try:
        days = int(args["days"]) if args.get("days") else None
        since, until = runtime.date_window(days, args.get("since"), args.get("until"),
                                           runtime.config()["import"]["lookback_days"])
    except ValueError as exc:
        return runtime.json_response({"error": str(exc)}, 400)
    try:
        out = import_window(since, until, runtime.truthy(args.get("dry_run")))
    except Exception as exc:  # never leak a stack trace to the caller
        log.exception("import failed")
        return runtime.json_response({"error": str(exc)[:400]}, 500)
    log.info("done: %s", {k: v for k, v in out.items() if k != "sample"})
    failed = out.get("result", {}).get("failed", 0)
    return runtime.json_response(out, 207 if failed else 200)


if functions_framework:
    spend_import = functions_framework.http(spend_import)


def _cli() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--dry-run", action="store_true", help="query OpenAI, do not push")
    a = p.parse_args()
    since, until = runtime.date_window(a.days, a.since, a.until, runtime.config()["import"]["lookback_days"])
    print(json.dumps(import_window(since, until, a.dry_run), indent=2))


if __name__ == "__main__":
    _cli()
