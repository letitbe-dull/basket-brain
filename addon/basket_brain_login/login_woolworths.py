"""Woolworths NZ stealth login via camoufox.

Persistent browser profile + direct /shop/securelogin entry to survive Akamai.
Dev knobs: BB_HEADLESS=false (watch the browser), BB_DEBUG_DIR (dump location).
"""

from __future__ import annotations

import contextlib
import logging
import os
import pathlib
from datetime import datetime

from camoufox.async_api import AsyncCamoufox
from me_check import confirm_signed_in

logger = logging.getLogger("basket_brain_login")


class AuthError(Exception):
    """Login failed — the shop rejected these credentials."""


class TransientLoginError(AuthError):
    """Login could not complete for a transient reason — a selector timeout,
    a bot-challenge, or an incomplete session handover. NOT a credential
    rejection: the caller must retry later and leave any live session intact,
    never trip reauth on this."""


WOOLIES_DOMAIN = "woolworths.co.nz"

# One selector, re-resolved each use, so a client-side re-render between finding
# the field and typing into it can't leave us holding a detached element.
EMAIL_SELECTOR = 'input[type="email"], input[name="username"]'
PASSWORD_SELECTOR = 'input[type="password"]:visible'


async def login_woolworths(email: str, password: str) -> list[dict]:
    """Perform a fresh Woolworths login and return harvested cookies.

    Returns a list of Playwright-format cookie dicts for the
    woolworths.co.nz domain, confirmed signed in by the GraphQL `Me` query.

    Raises AuthError on any failure to complete the login flow.
    """
    if not email or not password:
        raise AuthError("email and password are required")

    proxy_url = os.getenv("WOOLIES_PROXY")
    proxy_config = {"server": proxy_url} if proxy_url else None
    headless = os.getenv("BB_HEADLESS", "true").lower() != "false"

    # Persistent profile carries Akamai trust cookies; fresh browser looks like a bot.
    async with AsyncCamoufox(
        persistent_context=True,
        user_data_dir="/data/camoufox_profile",
        headless=headless,
        proxy=proxy_config,
    ) as context:
        page = await context.new_page()

        try:
            await _perform_login(page, email, password)
        except Exception as err:
            await _dump_debug(page, err, "woolies")
            raise

        cookies = await context.cookies()
        return [c for c in cookies if WOOLIES_DOMAIN in c.get("domain", "")]


async def _confirm_signed_in(page) -> bool:
    """Open the shop and ask the GraphQL `Me` query whether we're signed in.

    @param page: browser page
    @returns True when `Me` answers with a Customer
    """
    with contextlib.suppress(Exception):
        await page.goto("https://www.woolworths.co.nz", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)  # Let Akamai sensor script finish.
    return await confirm_signed_in(page)


# The homepage's third-party scripts can leave the "Sign in" link slow to
# render, and a consent overlay sometimes sits on top of it — both transient.
# Reload and re-look a few times before giving up rather than dying on one miss.
_SIGN_IN_ATTEMPTS = 3


async def _open_sign_in(page) -> None:
    """Homepage sign-in link hunt (fallback when direct entry didn't redirect)."""
    last_err: Exception | None = None
    for _ in range(_SIGN_IN_ATTEMPTS):
        await page.goto(
            "https://www.woolworths.co.nz", wait_until="domcontentloaded"
        )
        await page.wait_for_timeout(4000)  # Let Akamai sensor script finish.
        # A cookie/consent banner can cover the sign-in link — dismiss if present.
        with contextlib.suppress(Exception):
            await page.click(
                'button:has-text("Accept"), button:has-text("Got it")',
                timeout=2000,
            )
        try:
            await page.wait_for_selector(
                'a:has-text("Sign in")', timeout=15000, state="visible"
            )
            await page.click('a:has-text("Sign in")')
            return
        except Exception as e:  # noqa: BLE001 — transient, retried below
            last_err = e
            await page.wait_for_timeout(2000)
    raise TransientLoginError(
        f"Could not find sign-in link after {_SIGN_IN_ATTEMPTS} attempts: {last_err}"
    )


async def _perform_login(page, email: str, password: str) -> None:
    # Direct entry: /shop/securelogin bounces logged-out visitors straight to
    # Auth0. NO redirect means the persistent profile may still hold a live
    # session, so confirm it with `Me` and skip the login entirely.
    redirected = False
    with contextlib.suppress(Exception):
        await page.goto(
            "https://www.woolworths.co.nz/shop/securelogin",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(4000)  # Let Akamai sensor script finish.
        await page.wait_for_url(lambda url: "auth" in url, timeout=20000)
        redirected = True

    if not redirected:
        if await _confirm_signed_in(page):
            return
        await _open_sign_in(page)

    # Identifier step: email, then Continue. Woolworths uses Auth0's two-step
    # login, so pressing Enter here navigates to a separate password page.
    try:
        await page.wait_for_url(
            lambda url: "auth" in url or "login" in url, timeout=15000
        )
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)

        await page.wait_for_selector(EMAIL_SELECTOR, timeout=20000)
        await page.wait_for_timeout(4000)  # Let Akamai sensor script finish.
        # Selector-based fill/press re-resolve the element on each call, so an
        # Auth0 re-render can't strand us on a stale handle.
        await page.fill(EMAIL_SELECTOR, email)
        await page.wait_for_timeout(750)  # Humanised pacing between fill and press.
        await page.press(EMAIL_SELECTOR, "Enter")
        await page.wait_for_timeout(3000)
    except AuthError:
        raise
    except Exception as e:
        raise TransientLoginError(f"Email step failed: {e}") from e

    # Password step: wait for the field to arrive on the (navigated) password
    # page, then fill and submit. Longer timeout to cover the page transition.
    try:
        await page.wait_for_selector(PASSWORD_SELECTOR, timeout=15000)
        await page.wait_for_timeout(4000)  # Let Akamai sensor script finish.
        await page.fill(PASSWORD_SELECTOR, password)
        await page.wait_for_timeout(750)  # Humanised pacing between fill and press.
        await page.press(PASSWORD_SELECTOR, "Enter")
    except AuthError:
        raise
    except Exception as e:
        raise TransientLoginError(f"Password step failed: {e}") from e

    # Not always fatal — some flows land on a different subpath.
    with contextlib.suppress(Exception):
        await page.wait_for_url("https://www.woolworths.co.nz/**", timeout=15000)

    await page.wait_for_timeout(2000)

    if not await _confirm_signed_in(page):
        # Ambiguous — wrong creds OR a bot check. Treat as transient so a live
        # session is never nuked into reauth on what may be a bot challenge.
        raise TransientLoginError(
            "Login flow completed but Me did not answer as a signed-in customer — "
            "credentials may be wrong or bot check triggered."
        )


async def _dump_debug(page, err: Exception, prefix: str) -> None:
    """Save a screenshot + HTML + the current URL when a login fails.

    Best-effort only: a failure to capture must never mask the real error.
    Shared by both login flows; `prefix` names the files (e.g. "woolies").
    """
    try:
        out = pathlib.Path(os.getenv("BB_DEBUG_DIR", "/share/basket_brain_debug"))
        out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = out / f"{prefix}-{stamp}"

        # Capture each artefact independently so one failing doesn't lose the others.
        try:
            await page.screenshot(path=f"{base}.png", timeout=10000)
        except Exception as e:
            logger.warning(f"Failed to capture screenshot: {e}")

        try:
            base.with_suffix(".html").write_text(
                await page.content(), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to capture HTML: {e}")

        try:
            base.with_suffix(".txt").write_text(
                f"error: {err}\nurl: {page.url}\n", encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to capture error log: {e}")
    except Exception as e:
        logger.warning(f"Failed to create debug dump directory: {e}")
