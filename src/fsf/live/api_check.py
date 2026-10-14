"""Live public / vendor API verification. Local models are out of scope."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

PINNED = {
    "prodocux": "0.3.0rc7",
    "pdx-artifact-engine": "0.3.0a8",
    "pdx-adapter-media": "0.2.0a3",
}


def fetch_pypi_version(package: str, *, timeout_seconds: int = 30) -> dict[str, Any]:
    url = f"https://pypi.org/pypi/{package}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "free-studio-flow-live-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {"ok": False, "package": package, "error": str(exc)}
    except TimeoutError:
        return {"ok": False, "package": package, "error": "timeout"}
    version = body.get("info", {}).get("version")
    expected = PINNED[package]
    releases = body.get("releases", {})
    has_pin = expected in releases
    return {
        "ok": has_pin,
        "package": package,
        "pypi_latest": version,
        "pinned": expected,
        "pin_published": has_pin,
        "url": url,
    }


def ping_vendor_api(*, timeout_seconds: int = 30) -> dict[str, Any]:
    """Optional vendor/cloud API. Requires FSF_VENDOR_API_BASE and FSF_VENDOR_API_KEY.

    The key is sent as Bearer token and never returned in the report.
    """
    base = os.environ.get("FSF_VENDOR_API_BASE", "").strip().rstrip("/")
    key = os.environ.get("FSF_VENDOR_API_KEY", "").strip()
    if not base or not key:
        return {
            "ok": False,
            "configured": False,
            "error": "FSF_VENDOR_API_BASE / FSF_VENDOR_API_KEY not set",
        }
    url = f"{base}/models" if not base.endswith("/models") else base
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "free-studio-flow-live-check",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", 200)
            return {"ok": 200 <= int(status) < 300, "configured": True, "http_status": int(status)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "configured": True, "http_status": exc.code, "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "configured": True, "error": str(exc)}


def verify_apis() -> dict[str, Any]:
    """Ping published PDX package APIs; vendor video API if configured."""
    pypi = [fetch_pypi_version(name) for name in PINNED]
    vendor = ping_vendor_api()
    pypi_ok = all(item.get("ok") for item in pypi)
    # Vendor is extra: required only when configured.
    vendor_ok = True if not vendor.get("configured") else bool(vendor.get("ok"))
    return {
        "ok": pypi_ok and vendor_ok,
        "skipped_local": True,
        "pypi": pypi,
        "vendor": {
            "configured": bool(vendor.get("configured")),
            "ok": vendor.get("ok"),
            "http_status": vendor.get("http_status"),
            "error": vendor.get("error") if not vendor.get("ok") else None,
        },
    }
