"""Talks to the Basket Brain Login add-on.

Groups the add-on's Playwright cookies by domain so callers pick the exact host
— critical for Foodstuffs, where collapsing domains destroys the shop session.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

# Login can take a while: browser cold-start + Akamai handshake — plus the
# add-on serialises logins, so a request may queue behind another chain's.
_LOGIN_TIMEOUT = ClientTimeout(total=180)


class LoginError(Exception):
    """Generic login failure — service unreachable or unexpected response."""


class InvalidCredentialsError(LoginError):
    """The add-on completed the flow but the shop rejected the credentials."""


class LoginClient:
    def __init__(
        self, session: ClientSession, base_url: str, api_token: str
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token

    async def login(
        self, shop: str, email: str, password: str
    ) -> dict[str, dict[str, str]]:
        """Fetch a fresh cookie jar for one shop.

        Returns a domain-scoped `{domain: {name: value}}` cookie dict — each
        client picks the sub-jar for the host it's actually calling.
        """
        url = f"{self._base_url}/login"
        try:
            async with self._session.post(
                url,
                json={"shop": shop, "email": email, "password": password},
                headers={"Authorization": f"Bearer {self._api_token}"},
                timeout=_LOGIN_TIMEOUT,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 401:
                    raise InvalidCredentialsError(
                        body.get("error") or "Login rejected"
                    )
                if resp.status != 200:
                    raise LoginError(
                        f"Login add-on returned {resp.status}: "
                        f"{body.get('error') or body}"
                    )
        except (ClientError, TimeoutError) as err:
            raise LoginError(f"Login add-on unreachable: {err}") from err

        cookies = body.get("cookies") or []
        return _group_cookies_by_domain(cookies)


def _group_cookies_by_domain(
    cookies: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Group Playwright cookie dicts by domain into `{domain: {name: value}}`.

    Leading `.` on the Playwright domain is stripped for consistent keys.
    A name that appears on two domains stays in two separate sub-dicts —
    no silent overwrite.
    """
    grouped: dict[str, dict[str, str]] = {}
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not (
            isinstance(name, str)
            and isinstance(value, str)
            and isinstance(domain, str)
        ):
            continue
        key = domain.lstrip(".")
        grouped.setdefault(key, {})[name] = value
    return grouped
