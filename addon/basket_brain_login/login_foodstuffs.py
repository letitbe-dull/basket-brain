"""Foodstuffs NZ Club+ login via camoufox.

Handles PAK'nSAVE and New World — both use the same Club+ OAuth portal.
No Akamai wall; standard headless flow.

Flow:
  1. Navigate to Club+ login for the requested banner.
  2. If a returning-user screen appears, handle it:
       - "Continue" → reuse existing session.
       - "Login with another account" → force fresh login.
  3. Fill email → Continue → password → submit.
  4. Wait for the auth/callback redirect on the shop domain.
  5. Harvest refresh_token + session cookies for the shop and Club+ domains.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os

from camoufox.async_api import AsyncCamoufox
from login_woolworths import (  # shared with the Woolies flow
    AuthError,
    TransientLoginError,
    _dump_debug,
)

log = logging.getLogger("basket_brain_login")

SHOP_CONFIG = {
    "paknsave": {
        "banner": "PNS",
        "domain": "www.paknsave.co.nz",
        "callback_url": "https://www.paknsave.co.nz/auth/callback",
    },
    "newworld": {
        "banner": "MNW",
        "domain": "www.newworld.co.nz",
        "callback_url": "https://www.newworld.co.nz/auth/callback",
    },
}

CLUBPLUS_DOMAIN = "login.clubplus.co.nz"


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it (diagnostics only).

    `roles: ["SHOPPER"]` when signed in, `["ANONYMOUS"]` when not.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore stripped padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return {}


def _login_url(banner: str, callback_url: str) -> str:
    return (
        f"https://login.clubplus.co.nz/"
        f"?banner={banner}&channel=WEB&callback_url={callback_url}"
    )


async def login_foodstuffs(shop: str, email: str, password: str) -> list[dict]:
    """Log into Club+ for the given shop and return harvested cookies.

    Returns Playwright-format cookie dicts for the shop domain and Club+
    (the refresh_token cookie lives on Club+). Raises AuthError on failure.
    """
    shop = shop.lower()
    if shop not in SHOP_CONFIG:
        raise AuthError(f"Unknown Foodstuffs shop: {shop!r}")
    if not email or not password:
        raise AuthError("email and password are required")

    config = SHOP_CONFIG[shop]
    banner = config["banner"]
    domain = config["domain"]
    callback_url = config["callback_url"]

    proxy_url = os.getenv("FOODSTUFFS_PROXY")
    proxy_config = {"server": proxy_url} if proxy_url else None
    headless = os.getenv("BB_HEADLESS", "true").lower() != "false"

    async with AsyncCamoufox(headless=headless, proxy=proxy_config) as browser:
        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = await context.new_page()

        try:
            await _perform_login(page, banner, callback_url, email, password)
        except Exception as err:
            await _dump_debug(page, err, shop)
            raise

        all_cookies = await context.cookies()
        harvested = [
            c
            for c in all_cookies
            if domain in c.get("domain", "")
            or CLUBPLUS_DOMAIN in c.get("domain", "")
        ]
        _log_cookie_names(shop, all_cookies, harvested)
        return harvested


def _log_cookie_names(
    shop: str, all_cookies: list[dict], harvested: list[dict]
) -> None:
    """Log cookie names/domains (never values). Diagnostic for auth issues."""
    def _key(c: dict) -> tuple[str, str]:
        return (c.get("domain", ""), c.get("name", ""))

    def _fmt(cookies: list[dict]) -> str:
        return ", ".join(
            f"{c.get('name')}@{c.get('domain')}"
            f"{'[httpOnly]' if c.get('httpOnly') else ''}"
            for c in sorted(cookies, key=_key)
        ) or "<none>"

    log.info("COOKIE-DIAG %s: all domains  -> %s", shop, _fmt(all_cookies))
    log.info("COOKIE-DIAG %s: harvested    -> %s", shop, _fmt(harvested))
    log.info(
        "COOKIE-DIAG %s: %d harvested, %d httpOnly",
        shop,
        len(harvested),
        sum(1 for c in harvested if c.get("httpOnly")),
    )


async def _perform_login(
    page,
    banner: str,
    callback_url: str,
    email: str,
    password: str,
) -> None:
    url = _login_url(banner, callback_url)
    # "domcontentloaded", not the default "load": third-party scripts can keep
    # "load" from firing long past the 30s timeout.
    await page.goto(url, wait_until="domcontentloaded")
    # networkidle is best-effort; selector waits below are the real gate.
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=10000)
    await page.wait_for_timeout(1500)

    # Club+ may show an account-picker if a session cookie exists.
    # We run a fresh browser each call so this rarely fires, but handle it.
    handled_returning = await _handle_returning_user(page, email)

    if not handled_returning:
        await _email_step(page, email)
        await _password_step(page, password)

    # Landing on the shop host is NOT the finish line. Club+ redirects to
    # `/auth/callback?code=<uuid>` and the shop swaps that code for a real
    # session in the background. Harvesting on URL arrival grabs guest cookies
    # — we must poll until the shop itself reports SHOPPER.
    await _wait_until_signed_in(page, banner)


async def _wait_until_signed_in(
    page, banner: str, timeout_ms: int = 30000, poll_ms: int = 1000
) -> None:
    """Poll until the shop reports a real SHOPPER session. Raise on timeout.

    Don't check for `fs-user-token` — the shop mints one for guests too.
    The only reliable signal is `roles` inside the JWT.
    """
    deadline = timeout_ms
    claims: dict = {}
    roles = None
    while deadline > 0:
        claims = await _current_user_claims(page)
        roles = claims.get("roles")
        if isinstance(roles, list) and "SHOPPER" in roles:
            log.info(
                "TOKEN-DIAG %s: signed in as %s (%dms)",
                banner, claims.get("email"), timeout_ms - deadline,
            )
            return
        await page.wait_for_timeout(poll_ms)
        deadline -= poll_ms

    log.warning(
        "TOKEN-DIAG %s: still anonymous after %dms: roles=%r url=%s",
        banner, timeout_ms, roles, page.url,
    )
    raise TransientLoginError(
        f"Club+ signed in but the shop never completed the handover "
        f"(roles={roles!r} after {timeout_ms // 1000}s)."
    )


async def _current_user_claims(page) -> dict:
    try:
        user_info = await page.evaluate("""
            async () => {
                const resp = await fetch('/api/user/get-current-user', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}'
                });
                return await resp.json();
            }
        """)
    except Exception as e:  # noqa: BLE001
        # Page navigates mid-handover, killing in-flight evaluate — try again.
        log.debug("current-user probe failed (page likely navigating): %s", e)
        return {}

    if not isinstance(user_info, dict):
        return {}
    token = user_info.get("access_token")
    return _jwt_claims(token) if token else {}


async def _handle_returning_user(page, email: str) -> bool:
    """Handle the Club+ returning-user screen.

    Returns True if the session was reused, False if the screen was not shown.
    """
    try:
        continue_btn = await page.wait_for_selector(
            'button:has-text("Continue"):not([type="submit"]), '
            'a:has-text("Continue as")',
            timeout=3000,
            state="visible",
        )
        if continue_btn:
            await continue_btn.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(1000)
            return True
    except Exception:
        pass

    try:
        other_btn = await page.query_selector(
            'button:has-text("Login with another account"), '
            'a:has-text("Login with another account")'
        )
        if other_btn:
            await other_btn.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    return False


async def _email_step(page, email: str) -> None:
    try:
        email_input = await page.wait_for_selector(
            'input[type="email"]', timeout=45000, state="visible"
        )
        await email_input.fill(email)
        # Submit with Enter, not a button click: focus is already on the input,
        # so this needs nothing to locate and can't mis-click a not-yet-ready
        # button. The password step's wait_for_selector is the real gate for the
        # email->password transition — no fixed sleep here.
        await email_input.press("Enter")
    except Exception as e:
        raise TransientLoginError(f"Club+ email step failed: {e}") from e


async def _password_step(page, password: str) -> None:
    try:
        # This wait spans the whole email->password transition (Auth0 round
        # trip plus render), so it needs the same headroom as the email step
        # rather than a token 10s — a slow transition here reads as a hard
        # login failure and blanks the chain.
        password_input = await page.wait_for_selector(
            'input[type="password"]', timeout=45000, state="visible"
        )
        await password_input.fill(password)

        submit_btn = await page.query_selector('button[type="submit"]')
        if submit_btn:
            await submit_btn.click()
        else:
            await password_input.press("Enter")
    except Exception as e:
        raise TransientLoginError(f"Club+ password step failed: {e}") from e
