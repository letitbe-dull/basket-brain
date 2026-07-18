"""Serve the Basket Brain dashboard card from inside the integration.

The card is registered automatically — no manual resource entry needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers import start as ha_start

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "basket-brain-card.js"
URL_BASE = f"/{DOMAIN}_frontend"
CARD_URL = f"{URL_BASE}/{CARD_FILENAME}"
_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card and register it as a Lovelace resource. Safe to call twice.

    A Lovelace resource — not add_extra_js_url. The frontend loads and awaits
    resources before it renders a view; add_extra_js_url is fire-and-forget, so
    Lovelace can try to build the card before the module has defined it and show
    a bare "Configuration error".

    The cache-buster is the card file's own modification time, not the
    integration version: during development the version never moves, so a
    version-stamped URL would leave the browser happily serving a stale card.
    """
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True

    path = Path(__file__).parent / "frontend"
    card = path / CARD_FILENAME

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                url_path=URL_BASE,
                path=str(path),
                cache_headers=False,
            )
        ]
    )

    async def _register_resource(_event=None) -> None:
        # Imported late: Lovelace may not be loaded when this module is.
        from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE

        lovelace = hass.data.get(LOVELACE_DATA)
        if lovelace is None:
            _LOGGER.warning("Lovelace is not ready; add %s manually", CARD_URL)
            return
        if lovelace.resource_mode != MODE_STORAGE:
            _LOGGER.warning(
                "Lovelace is in YAML mode; add this resource yourself: "
                "url: %s, type: module",
                CARD_URL,
            )
            return

        try:
            version = await hass.async_add_executor_job(
                lambda: int(card.stat().st_mtime)
            )
        except OSError:
            version = 0
        url = f"{CARD_URL}?v={version}"

        resources = lovelace.resources
        await resources.async_get_info()  # make sure the collection is loaded

        existing = next(
            (
                item
                for item in resources.async_items()
                if item.get("url", "").split("?")[0] == CARD_URL
            ),
            None,
        )
        if existing is None:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Registered the Basket Brain card resource: %s", url)
        elif existing.get("url") != url:
            await resources.async_update_item(existing["id"], {"url": url})
            _LOGGER.info("Updated the Basket Brain card resource: %s", url)

    ha_start.async_at_started(hass, _register_resource)
