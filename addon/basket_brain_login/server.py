"""Basket Brain Login HTTP service.

POST /login {shop, email, password} → {"shop": ..., "cookies": [...]}

Stateless: each request logs in fresh via camoufox and returns the harvested
cookie jar. Logins are serialised — concurrent browsers in one container kill
each other (TargetClosedError). Supported shops: woolworths, paknsave, newworld.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os

from aiohttp import web
from login_foodstuffs import login_foodstuffs
from login_woolworths import AuthError, TransientLoginError, login_woolworths

PORT = 8099

# Shared secret set by the integration via add-on options. Every add-on on the
# Supervisor network can reach this port, so /login refuses anyone without it.
# Fail closed: no token configured means no logins, rather than an open door.
API_TOKEN = os.environ.get("API_TOKEN", "")

log = logging.getLogger("basket_brain_login")

SUPPORTED_SHOPS = {"woolworths", "paknsave", "newworld"}

# One headless browser at a time. Two concurrent camoufox launches in this
# container stomp on each other and one dies with TargetClosedError.
_LOGIN_LOCK = asyncio.Lock()


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _authorised(request: web.Request) -> bool:
    """Check the bearer token in constant time."""
    if not API_TOKEN:
        return False
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented, API_TOKEN)


async def handle_login(request: web.Request) -> web.Response:
    if not _authorised(request):
        if not API_TOKEN:
            log.error("Rejected login: no api_token configured on the add-on")
            return web.json_response(
                {"error": "add-on has no api_token configured"}, status=503
            )
        log.warning("Rejected login: bad or missing token")
        return web.json_response({"error": "unauthorised"}, status=403)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    shop = (payload.get("shop") or "").strip().lower()
    email = payload.get("email") or ""
    password = payload.get("password") or ""

    if shop not in SUPPORTED_SHOPS:
        return web.json_response(
            {"error": f"unknown shop: {shop!r}", "supported": sorted(SUPPORTED_SHOPS)},
            status=400,
        )
    if not email or not password:
        return web.json_response(
            {"error": "email and password are required"}, status=400
        )

    if shop == "woolworths":
        try:
            async with _LOGIN_LOCK:
                cookies = await login_woolworths(email, password)
        except TransientLoginError as e:
            # Transient (timeout / bot-challenge) — 502, not 401, so the client
            # treats it as retryable and never trips reauth on a live session.
            log.warning("Woolworths login transient failure: %s", e)
            return web.json_response({"error": str(e)}, status=502)
        except AuthError as e:
            log.warning("Woolworths login failed: %s", e)
            return web.json_response({"error": str(e)}, status=401)
        except Exception as e:  # noqa: BLE001
            log.exception("Woolworths login crashed")
            return web.json_response({"error": f"internal error: {e}"}, status=500)
        return web.json_response({"shop": "woolworths", "cookies": cookies})

    try:
        async with _LOGIN_LOCK:
            cookies = await login_foodstuffs(shop, email, password)
    except TransientLoginError as e:
        # Transient (timeout / bot-challenge) — 502, not 401, so the client
        # treats it as retryable and never trips reauth on a live session.
        log.warning("Foodstuffs (%s) login transient failure: %s", shop, e)
        return web.json_response({"error": str(e)}, status=502)
    except AuthError as e:
        log.warning("Foodstuffs (%s) login failed: %s", shop, e)
        return web.json_response({"error": str(e)}, status=401)
    except Exception as e:  # noqa: BLE001
        log.exception("Foodstuffs (%s) login crashed", shop)
        return web.json_response({"error": f"internal error: {e}"}, status=500)
    return web.json_response({"shop": shop, "cookies": cookies})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/login", handle_login)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
