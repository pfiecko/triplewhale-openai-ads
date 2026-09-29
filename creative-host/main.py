#!/usr/bin/env python3
"""
OpenAI ad creative host (Cloud Function, gen2, public HTTPS).

OpenAI creative image URLs are signed and expire within about a day, so handing them
to Triple Whale leaves a broken thumbnail once they lapse. The spend import copies
each creative into a private GCS bucket keyed by OpenAI file_id; this function
serves those bytes at a stable public URL Triple Whale can use as `ad_image_url`:

    GET https://<this-function>/<file_id>  →  image bytes

The bucket stays private (public access prevention can stay on). file_ids are
content-addressed, so responses are cached as immutable.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import runtime
except ImportError:  # running from a checkout: shared modules live in ../lib
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import runtime

try:
    import functions_framework
except ImportError:
    functions_framework = None

log = runtime.setup_logging("openai-ad-creative-host")

_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_bucket = None


def _get_bucket():
    global _bucket
    if _bucket is None:
        from google.cloud import storage
        _bucket = storage.Client().bucket(runtime.config()["creatives"]["bucket"])
    return _bucket


def serve_creative(request):
    if request.method != "GET":
        return ("method not allowed", 405)
    file_id = (request.path or "").strip("/")
    if not _FILE_ID.match(file_id):  # flat ids only: no paths, no traversal
        return ("not found", 404)
    blob = _get_bucket().get_blob(file_id)
    if blob is None:
        return ("not found", 404)
    return (blob.download_as_bytes(), 200, {
        "Content-Type": blob.content_type or "image/png",
        "Cache-Control": "public, max-age=31536000, immutable",
        "Access-Control-Allow-Origin": "*",
    })


if functions_framework:
    serve_creative = functions_framework.http(serve_creative)
