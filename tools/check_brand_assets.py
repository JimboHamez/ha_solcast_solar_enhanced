#!/usr/bin/env python3
"""Check that this integration's brand assets are being served by a running
Home Assistant instance via the brands proxy API (HA 2026.3+).

HA serves integration brand imagery it finds in
``custom_components/<domain>/brand/`` through a local endpoint:

    /api/brands/integration/<domain>/<image>

This script curls that endpoint for each shipped asset and reports whether the
response is a real PNG (the local asset was picked up) rather than a 404 or the
generic placeholder (not picked up). It is a deployment/verification aid only —
it talks to *your* HA over HTTP and ships nothing back.

Examples
--------
    # Local HA, asset endpoints are public (no token needed on most setups)
    python tools/check_brand_assets.py --host http://homeassistant.local:8123

    # If your instance requires auth, pass a long-lived access token
    python tools/check_brand_assets.py --host https://ha.example.com \
        --token "$HA_TOKEN"

    # Verify a different integration's assets
    python tools/check_brand_assets.py --host http://localhost:8123 \
        --domain solcast_solar

Requirements: standard library only (urllib) — no extra install.
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

# Asset filenames this integration ships in custom_components/<domain>/brand/.
# Optional dark/extra variants are checked too, but a miss on them is not a
# failure (we don't ship them by default).
REQUIRED_ASSETS = ("icon.png", "icon@2x.png", "logo.png")
OPTIONAL_ASSETS = ("dark_icon.png", "dark_icon@2x.png", "dark_logo.png", "logo@2x.png")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _fetch(url: str, token: str | None, timeout: float) -> tuple[int, str, bytes]:
    """Return (status, content_type, body) for a GET, following redirects."""
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type", "") if err.headers else "", err.read() or b""


def _check_one(base: str, domain: str, name: str, token: str | None, timeout: float) -> bool:
    url = f"{base}/api/brands/integration/{domain}/{name}"
    try:
        status, ctype, body = _fetch(url, token, timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
        print(f"  ✗ {name:18s} ERROR  {err}")
        return False

    is_png = body[:8] == PNG_MAGIC
    ok = status == 200 and is_png
    mark = "✓" if ok else "✗"
    detail = f"HTTP {status}  {ctype or '-'}  {len(body)} bytes"
    if status == 200 and not is_png:
        detail += "  (not a PNG — likely a placeholder/redirect, asset NOT picked up)"
    print(f"  {mark} {name:18s} {detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="HA base URL, e.g. http://homeassistant.local:8123")
    parser.add_argument("--token", default=None, help="Long-lived access token (only if your instance requires auth)")
    parser.add_argument("--domain", default="solcast_solar_enhanced", help="Integration domain (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds (default: %(default)s)")
    args = parser.parse_args(argv)

    base = args.host.rstrip("/")
    print(f"Brands proxy check: {base}/api/brands/integration/{args.domain}/<image>\n")

    print("Required assets:")
    # Evaluate every asset (list, not a generator) so all statuses print even
    # when an earlier one fails — short-circuiting would hide later results.
    required_ok = all(
        [_check_one(base, args.domain, name, args.token, args.timeout) for name in REQUIRED_ASSETS]
    )

    print("\nOptional assets (a miss here is fine — not shipped by default):")
    for name in OPTIONAL_ASSETS:
        _check_one(base, args.domain, name, args.token, args.timeout)

    print()
    if required_ok:
        print("PASS — all required brand assets are served by the proxy. The logo/icon should render in HA.")
        return 0
    print(
        "FAIL — one or more required assets were not served as PNGs.\n"
        "  Checklist: HA >= 2026.3, integration deployed from a build that includes brand/,\n"
        "  HA restarted after deploy, and the browser hard-refreshed (brand images cache hard)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
