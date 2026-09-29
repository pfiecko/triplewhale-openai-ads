from types import SimpleNamespace
from unittest import mock

import runtime
from conftest import load_function

main = load_function("spend-import")


class FakeAPI:
    """Two ads in one ad group; ad_2 has a sparse list item and needs a full fetch."""

    def campaigns(self):
        return [{"id": "cmp_1", "name": "Launch", "status": "active"}]

    def ad_groups(self, campaign_id):
        return [{"id": "grp_1", "name": "Broad", "status": "paused"}]

    def ads(self, ad_group_id):
        return [{"id": "ad_1", "name": "Hero", "status": "active",
                 "creative": {"image_url": "https://signed/1", "file_id": "file_1"}},
                {"id": "ad_2"}]

    def ad(self, ad_id):
        return {"id": ad_id, "status": "active", "creative": {"title": "Headline 2", "image_url": None, "file_id": "file_2"}}

    def daily_insights(self, ad_id, days):
        data = {"ad_1": [{"readable_time": "2026-09-01", "spend": "12.5", "clicks": "4", "impressions": "900"},
                         {"readable_time": "2026-09-03", "spend": "7", "clicks": "1", "impressions": "300"},
                         {"readable_time": "2026-08-20", "spend": "99", "clicks": "9", "impressions": "9"}],
                "ad_2": []}
        return data[ad_id]


def test_collect_rows_keeps_hierarchy_and_window():
    store = main.CreativeStore({})
    rows = main.collect_rows(FakeAPI(), "2026-09-01", "2026-09-03", 7, store)
    assert [r["date"] for r in rows] == ["2026-09-01", "2026-09-03"]   # 08-20 is outside the window
    assert rows[0]["adset"] == {"id": "grp_1", "name": "Broad", "status": "PAUSED"}
    assert rows[0]["ad"]["name"] == "Hero" and rows[0]["ad"]["ad_image_url"] is None


def test_records_are_zero_filled_across_the_window():
    rows = main.collect_rows(FakeAPI(), "2026-09-01", "2026-09-03", 7, main.CreativeStore({}))
    recs = main.to_records(rows, "2026-09-01", "2026-09-03", runtime.config())
    assert [(r["event_date"], r["metrics"]["spend"]) for r in recs] == [
        ("2026-09-01", 12.5), ("2026-09-02", 0), ("2026-09-03", 7.0)]
    assert recs[0]["channel"] == "openai-ads" and recs[0]["metrics"]["clicks"] == 4


def test_ad_details_fetches_full_ad_when_sparse():
    name, image, file_id, status = main.ad_details(FakeAPI(), {"id": "ad_2"})
    assert (name, image, file_id, status) == ("Headline 2", None, "file_2", "ACTIVE")


def test_creative_store_returns_stable_host_url():
    blob = mock.Mock(exists=mock.Mock(return_value=False))
    store = main.CreativeStore({"bucket": "b", "host_base": "https://host/"})
    store.bucket = mock.Mock(blob=mock.Mock(return_value=blob))
    resp = mock.Mock(content=b"png", headers={"Content-Type": "image/png; charset=binary"})
    with mock.patch.object(main.requests, "get", return_value=resp) as get:
        assert store.url("file_1", "https://signed/1") == "https://host/file_1"
        assert store.url("file_1", "https://signed/1") == "https://host/file_1"
    get.assert_called_once()                       # cached within a run
    blob.upload_from_string.assert_called_once_with(b"png", content_type="image/png")


def test_creative_store_disabled_without_config():
    assert main.CreativeStore(None).url("file_1", "https://signed/1") is None


def test_http_handler_dry_run(monkeypatch):
    monkeypatch.setenv("OPENAI_ADS_READ_TOKEN", "t")
    monkeypatch.setattr(main, "OpenAIAds", lambda token: FakeAPI())
    body, status, _ = main.spend_import(SimpleNamespace(args={"since": "2026-09-01", "until": "2026-09-03", "dry_run": "1"}))
    assert status == 200 and '"records": 3' in body and '"dry_run": true' in body
