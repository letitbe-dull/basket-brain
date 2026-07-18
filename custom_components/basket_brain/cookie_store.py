"""Persisted cookie jars for the supermarket sessions.

Each login costs ~30s of headless browser, so the add-on's jar is cached in
`.storage` (per config entry, 0600) and reused until a day old or rejected.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1

# Discard a jar once it reaches this age, even if the shop still accepts it.
# Reactive expiry (a 401 on a real request) is the real driver now — this is
# just an age backstop for a jar that has quietly gone stale. It sits above the
# 24h coordinator interval so a tick can't discard a not-quite-day-old jar and
# force a browser login for no reason.
MAX_AGE_SECONDS = 25 * 3600

type CookieJar = dict[str, dict[str, str]]


class CookieStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.cookies.{entry_id}", private=True
        )
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Read the saved jars. Missing file just means no saved jars."""
        self._data = await self._store.async_load() or {}

    def get(self, chain: str) -> CookieJar | None:
        """Return the saved jar for a chain, or None if absent or too old."""
        saved = self._data.get(chain)
        if not saved:
            return None
        if time.time() - saved.get("saved_at", 0) > MAX_AGE_SECONDS:
            return None
        return saved.get("cookies") or None

    async def async_set(self, chain: str, cookies: CookieJar) -> None:
        self._data[chain] = {"cookies": cookies, "saved_at": time.time()}
        await self._store.async_save(self._data)

    async def async_drop(self, chain: str) -> None:
        """Forget a chain's jar (it was rejected)."""
        if self._data.pop(chain, None) is not None:
            await self._store.async_save(self._data)
