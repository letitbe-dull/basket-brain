"""Saved cookie jars must expire — a stale jar means a silently dead session."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.basket_brain import cookie_store
from custom_components.basket_brain.cookie_store import MAX_AGE_SECONDS, CookieStore

JAR = {"www.paknsave.co.nz": {"session": "S"}}


async def test_roundtrip(hass: HomeAssistant) -> None:
    store = CookieStore(hass, "entry1")
    await store.async_load()
    await store.async_set("paknsave", JAR)

    assert store.get("paknsave") == JAR


async def test_unknown_chain_is_none(hass: HomeAssistant) -> None:
    store = CookieStore(hass, "entry1")
    await store.async_load()

    assert store.get("woolworths") is None


async def test_stale_jar_is_not_trusted(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CookieStore(hass, "entry1")
    await store.async_load()
    await store.async_set("paknsave", JAR)

    now = cookie_store.time.time()
    monkeypatch.setattr(
        cookie_store.time, "time", lambda: now + MAX_AGE_SECONDS + 1
    )

    assert store.get("paknsave") is None


async def test_drop(hass: HomeAssistant) -> None:
    store = CookieStore(hass, "entry1")
    await store.async_load()
    await store.async_set("paknsave", JAR)

    await store.async_drop("paknsave")

    assert store.get("paknsave") is None
