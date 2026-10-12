# triplewhale-openai-ads

Two-way **OpenAI Ads** (ChatGPT ads) integration for Shopify stores that report in **Triple Whale**:

- **Spend import:** OpenAI Ads Advertiser API → Triple Whale, so OpenAI Ads spend, clicks and
  impressions sit next to Meta and Google, with a campaign → ad group → ad hierarchy and working
  ad thumbnails.
- **Conversions:** Shopify → OpenAI Ads Conversions API plus the browser pixel, deduplicated and
  with enhanced conversions (hashed user data).

Part of a family of Triple Whale connectors that share one layout and one Data-In client:
[triplewhale-shop-campaigns](https://github.com/pfiecko/triplewhale-shop-campaigns) ·
[triplewhale-roku-ads](https://github.com/pfiecko/triplewhale-roku-ads) ·
[triplewhale-openai-ads](https://github.com/pfiecko/triplewhale-openai-ads) ·
[triplewhale-gravity-ads](https://github.com/pfiecko/triplewhale-gravity-ads)

```
               spend import                                         conversions
OpenAI Ads API  campaigns → ad groups → ads → daily insights    Shopify checkout (custom pixel)
      │                                                           │  oaiq order_created (browser) ──► OpenAI
      ▼                                                           │  POST /track + X-Pixel-Secret
openai-ads-tw-import ──► Triple Whale Data-In /ads                ▼
      │ creative image (signed URL, expires ~1 day)              openai-ads-conversion-proxy ──CAPI──► OpenAI
      ▼                                                             hash email/external id · IP + UA
private GCS bucket ◄── openai-ad-creative-host ◄── Triple Whale thumbnail requests
```

## Spend import

| Data-In field | OpenAI source |
|---|---|
| campaign | campaign `id` / `name` / `status` |
| adset | ad group `id` / `name` / `status` |
| ad | ad `id` / `name` (falls back to `creative.title`) / `status` |
| `ad.ad_image_url` | re-hosted creative (see below) |
| `spend`, `clicks`, `impressions` | ad insights, daily granularity |
| `conversions`, `conversion_value`, `visits` | 0: purchases come from the Shopify side |

Status maps `active` → `ACTIVE` and everything else → `PAUSED`. Archived campaigns are skipped.

Each run re-sends the last `lookback_days` (default 7), so days OpenAI restates are overwritten.
Ads that drop to zero inside the window are re-sent as 0. The insights call returns the most
recent `insights_days` daily rows per ad, and that is also the furthest back a backfill can reach.

**Thumbnails.** `creative.image_url` is a signed URL that expires within about a day, so passing
it to Triple Whale leaves a broken image. The import downloads each creative while its URL is
valid, stores it in a private GCS bucket keyed by `file_id`, and sends Triple Whale a stable URL
served by `creative-host`. `file_id`s are content-addressed, so each image is stored once and
cached as immutable. The bucket can keep public access prevention on.

## Conversions

| OpenAI event | Fires from | Trigger |
|---|---|---|
| `page_viewed` | GTM `gtm/02-page-viewed.html` | DOM Ready, all pages |
| `contents_viewed` | GTM `gtm/03-contents-viewed.html` | your GA4 `view_item` event |
| `items_added` | GTM `gtm/04-items-added.html` | your GA4 `add_to_cart` event |
| `checkout_started` | Shopify custom pixel | `checkout_started` |
| `order_created` (browser) | Shopify custom pixel | `checkout_completed` |
| `order_created` (server) | conversion proxy → CAPI | `checkout_completed` → `/track` |

- **Deduplication:** browser and server events share `event id = order id`.
- **Click id:** `gtm/01-init.html` stores the landing page's `oppref` in a first-party cookie.
  The pixel reads it and forwards it with the server event.
- **Enhanced conversions:** the pixel hashes the email in the browser before handing it to the
  SDK. The proxy sends `email_sha256` and `external_id_sha256` (SHA-256 of the trimmed, lowercased
  value), raw `country`, `city`, `zip_code`, and the buyer's IP and User-Agent taken from the
  request.
- **Amounts** are integer minor units (cents).
- **Optional scope filter:** `conversions.allowed_currencies` / `allowed_countries` and the
  matching constants in the pixel limit which orders are reported. Empty lists report everything.

## Setup

Requirements: a GCP project with Cloud Functions, Cloud Run, Cloud Scheduler, Cloud Storage and
Secret Manager enabled; `gcloud`; Python 3 with PyYAML.

1. **Config:** `cp config.example.yaml config.yaml` and fill it in.
2. **Tokens.** OpenAI Ads service-account tokens carry a single scope each, so create two: one
   that can read ads (spend import) and one that can write conversions (proxy).
3. **Secrets, bucket and IAM** (names as in your config):
   ```bash
   PID=my-gcp-project; SA=tw-importer@$PID.iam.gserviceaccount.com
   add() { gcloud secrets create "$1" --project=$PID 2>/dev/null || true
           printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- --project=$PID
           gcloud secrets add-iam-policy-binding "$1" --project=$PID \
             --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null; }
   add openai-ads-read-token   '…'
   add openai-ads-capi-token   '…'
   add openai-ads-pixel-secret "$(openssl rand -hex 32)"
   add triplewhale-api-key     '…'   # scope "Ads: Write"
   gcloud storage buckets create gs://$PID-openai-ad-creatives --project=$PID \
     --location=europe-west1 --uniform-bucket-level-access --public-access-prevention
   gcloud storage buckets add-iam-policy-binding gs://$PID-openai-ad-creatives \
     --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
   ```
4. **Creative host:** run `./deploy.sh creative-host` and put its URL in `creatives.host_base`.
5. **Spend import:**
   - `./deploy.sh spend-import`
   - dry run: `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL?days=7&dry_run=1"`
   - `./deploy.sh spend-import --schedule` (daily by default)
6. **Conversions:**
   - `./deploy.sh conversion-proxy`
   - fill in the constants in `shopify-pixel/pixel.js` and add it under Shopify Admin →
     Settings → Customer events → Add custom pixel
   - add the GTM tags, with a constant variable `{{const - oai pixel id}}`
   - optionally start with `conversions.validate_only: true`

## Local development

```bash
pip install -r requirements-dev.txt
pytest

# live OpenAI query, nothing sent to Triple Whale. Secrets come from env vars named after
# the secret id, so no GCP access is needed (thumbnails are skipped without a bucket).
export OPENAI_ADS_READ_TOKEN='…'
python spend-import/main.py --days 7 --dry-run
```

## Gotchas

- **Request `spend` and `clicks` explicitly.** The insights endpoint does not return them by
  default: pass `fields[]=spend&fields[]=clicks`.
- **Two tokens, two scopes.** A read token cannot send conversions, and a conversions token
  cannot read reports.
- **Do not also push orders.** Triple Whale already receives Shopify orders natively. Posting them
  again through Data-In `/orders` with an `openai-ads` source creates duplicates, so this repo
  sends spend only.
- **The proxy returns 502 when OpenAI rejects an event**, so rejections show up in request logs.
- **The pixel secret is public.** Anyone can read it in the pixel source, so treat it as a spam
  filter. The proxy re-validates everything it forwards.

## Layout

| Path | What |
|---|---|
| `spend-import/main.py` | OpenAI Ads API walk, creative re-hosting, Triple Whale mapping, CLI |
| `conversion-proxy/main.py` | pixel → OpenAI Conversions API proxy |
| `creative-host/main.py` | serves re-hosted creatives from the private bucket |
| `shopify-pixel/pixel.js` | Shopify custom pixel: order_created (browser + CAPI), checkout_started |
| `gtm/` | storefront GTM tags: init + oppref cookie, page_viewed, contents_viewed, items_added |
| `lib/` | shared modules, identical across the connector family |
| `deploy.sh` | `./deploy.sh spend-import\|conversion-proxy\|creative-host [--schedule]` |
| `tests/` | unit tests |

## License

[MIT](LICENSE)
