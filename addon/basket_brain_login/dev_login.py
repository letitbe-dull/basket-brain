"""Local dev harness: log in to a shop outside HA and Docker, and watch it.

Set your credentials as env vars (never in this file), then run. Headful by
default here so you can watch Firefox drive the login:

  PowerShell:
    $env:WOOLIES_EMAIL="you@example.com"      # Woolworths
    $env:WOOLIES_PASSWORD="..."
    $env:FOODSTUFFS_EMAIL="you@example.com"    # PAK'nSAVE + New World (shared Club+)
    $env:FOODSTUFFS_PASSWORD="..."
    $env:BB_HEADLESS="false"

    python dev_login.py                 # woolworths (default)
    python dev_login.py paknsave
    python dev_login.py newworld

On failure a screenshot + HTML land in ./debug (override with BB_DEBUG_DIR).
"""

from __future__ import annotations

import asyncio
import os
import sys

from login_foodstuffs import login_foodstuffs
from login_woolworths import AuthError, login_woolworths

# Watch it by default when run as a dev harness; the add-on stays headless.
os.environ.setdefault("BB_HEADLESS", "false")

SHOPS = ("woolworths", "paknsave", "newworld")


async def main() -> int:
    shop = (sys.argv[1] if len(sys.argv) > 1 else "woolworths").lower()
    if shop not in SHOPS:
        print(f"Unknown shop {shop!r}. Choose one of: {', '.join(SHOPS)}")
        return 2

    if shop == "woolworths":
        email = os.getenv("WOOLIES_EMAIL")
        password = os.getenv("WOOLIES_PASSWORD")
        creds_hint = "WOOLIES_EMAIL / WOOLIES_PASSWORD"
    else:
        email = os.getenv("FOODSTUFFS_EMAIL")
        password = os.getenv("FOODSTUFFS_PASSWORD")
        creds_hint = "FOODSTUFFS_EMAIL / FOODSTUFFS_PASSWORD"

    if not email or not password:
        print(f"Set {creds_hint} first.")
        return 2

    try:
        if shop == "woolworths":
            cookies = await login_woolworths(email, password)
        else:
            cookies = await login_foodstuffs(shop, email, password)
    except AuthError as e:
        print(f"{shop} LOGIN FAILED: {e}")
        print(f"See {os.getenv('BB_DEBUG_DIR', 'debug')}/ for a screenshot + HTML.")
        return 1

    names = sorted(c["name"] for c in cookies)
    print(f"OK — {shop}: {len(cookies)} cookies")
    print("cookies:", ", ".join(names))
    # The token each flow relies on downstream.
    key = "XSRF-TOKEN" if shop == "woolworths" else "refresh_token"
    print(f"{key} present:", any(n == key for n in names))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
