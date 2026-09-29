/**
 * OpenAI Ads — Shopify Custom Pixel: browser pixel + server-side Conversions API.
 *
 * Runs in Shopify's sandboxed web-pixel environment and covers the checkout-stage
 * events GTM cannot fire, because GTM does not run on Shopify's hosted checkout:
 *
 *   order_created     browser (oaiq) + server CAPI (via the conversion proxy), both with
 *                     event id = order id so OpenAI deduplicates them
 *   checkout_started  browser (oaiq)
 *
 * Storefront events (page_viewed, contents_viewed, items_added) are GTM tags, see ../gtm/.
 * The GTM init tag also stores the landing page's oppref click id in a first-party
 * cookie; this pixel reads it and forwards it with the server-side event.
 *
 * Setup: Shopify Admin → Settings → Customer events → Add custom pixel → paste this
 * file → fill in the constants below → Save → Connect.
 */

const PIXEL_ID     = 'YOUR_PIXEL_ID';
const CF_ENDPOINT  = 'https://YOUR-CONVERSION-PROXY-URL/track';  // printed by ./deploy.sh conversion-proxy
const PIXEL_SECRET = '__PIXEL_SECRET__';                        // value of the pixel_secret secret
const OPPREF_COOKIE = 'oai_oppref';                             // must match the GTM init tag

// Optional scope filter, mirrored server-side in config.yaml. Empty = every order.
const ALLOWED_CURRENCIES = [];
const ALLOWED_COUNTRIES  = [];

// ---------------------------------------------------------------------------
// Load the OpenAI Ads pixel SDK. oaiq() queues calls until the SDK has loaded.
// ---------------------------------------------------------------------------
(function (w, d, s, u) {
  if (w.oaiq) return;
  const q = function (...args) { q.q.push(args); };
  q.q = [];
  w.oaiq = q;
  const js = d.createElement(s);
  js.async = true;
  js.src = u;
  const f = d.getElementsByTagName(s)[0];
  (f ? f.parentNode : d.head).insertBefore(js, f || null);
})(window, document, 'script', 'https://bzrcdn.openai.com/sdk/oaiq.min.js');

oaiq('init', { pixelId: PIXEL_ID });

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Storefront cookie. The pixel sandbox cannot read document.cookie of the top page,
 *  so use the Web Pixels API and fall back to document.cookie outside the sandbox. */
async function readCookie(name) {
  try {
    if (typeof browser !== 'undefined' && browser.cookie) return (await browser.cookie.get(name)) || null;
  } catch (_) {}
  const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
  return match ? decodeURIComponent(match[2]) : null;
}

async function sha256Hex(value) {
  if (!value) return null;
  const bytes = new TextEncoder().encode(String(value).trim().toLowerCase());
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function addressOf(checkout) {
  return checkout.shippingAddress || checkout.billingAddress || {};
}

function currencyOf(checkout) {
  return checkout?.totalPrice?.currencyCode || checkout?.currencyCode || '';
}

function checkoutInScope(checkout) {
  const currency = currencyOf(checkout);
  const country  = addressOf(checkout).countryCode || '';
  if (ALLOWED_CURRENCIES.length && !ALLOWED_CURRENCIES.includes(currency)) return false;
  if (ALLOWED_COUNTRIES.length && country && !ALLOWED_COUNTRIES.includes(country)) return false;
  return true;
}

/** Enhanced conversions: email hashed in the browser, geo sent raw. */
async function buildUserData(checkout) {
  const addr = addressOf(checkout);
  const user = {};
  const emailHash = await sha256Hex(checkout.email);
  if (emailHash) user.email_sha256 = emailHash;
  if (addr.countryCode) user.country = String(addr.countryCode).trim().toLowerCase();
  if (addr.city)        user.city = String(addr.city).trim().toLowerCase();
  if (addr.zip)         user.zip_code = String(addr.zip).trim().toLowerCase();
  return user;
}

function contentsOf(lineItems) {
  return (lineItems || []).map((item) => ({
    id:           item.variant?.sku || String(item.variant?.id || item.id || ''),
    name:         item.title || item.variant?.product?.title || '',
    content_type: 'product',
    quantity:     item.quantity || 1,
  }));
}

function amountCents(checkout) {
  return Math.round(parseFloat(checkout.totalPrice?.amount || 0) * 100);
}

// ---------------------------------------------------------------------------
// order_created: browser oaiq + server CAPI with a shared event id
// ---------------------------------------------------------------------------
analytics.subscribe('checkout_completed', async (event) => {
  const checkout = event?.data?.checkout;
  const order    = checkout?.order;
  if (!order?.id || !checkoutInScope(checkout)) return;

  const eventId  = String(order.id);
  const currency = currencyOf(checkout);
  const amount   = amountCents(checkout);
  const contents = contentsOf(checkout.lineItems);
  const oppref   = await readCookie(OPPREF_COOKIE);
  const addr     = addressOf(checkout);

  // Re-init with the user object so this measure call carries the identity signal.
  const user = await buildUserData(checkout);
  if (Object.keys(user).length) oaiq('init', { pixelId: PIXEL_ID, user });

  oaiq('measure', 'order_created',
    { type: 'contents', amount, currency, contents },
    { event_id: eventId, ...(oppref ? { oppref } : {}) });

  // Fire and forget: the browser event already covers the conversion if this fails.
  try {
    await fetch(CF_ENDPOINT, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-Pixel-Secret': PIXEL_SECRET },
      body: JSON.stringify({
        event_id:     eventId,
        order_id:     eventId,
        amount_cents: amount,
        currency,
        oppref:       oppref || null,
        source_url:   event?.context?.document?.location?.href || '',
        email:        checkout.email || null,
        external_id:  order.customer?.id || null,
        country:      addr.countryCode || null,
        city:         addr.city || null,
        zip_code:     addr.zip || null,
        contents,
      }),
      keepalive: true,
    });
  } catch (_) {}
});

// ---------------------------------------------------------------------------
// checkout_started: browser only (GTM cannot see Shopify's checkout)
// ---------------------------------------------------------------------------
analytics.subscribe('checkout_started', (event) => {
  const checkout = event?.data?.checkout;
  if (!checkout || !checkoutInScope(checkout)) return;
  oaiq('measure', 'checkout_started', {
    type:     'contents',
    amount:   amountCents(checkout),
    currency: currencyOf(checkout),
    contents: contentsOf(checkout.lineItems),
  });
});
