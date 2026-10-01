from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant.components.hassio import AddonError
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.hassio import is_hassio
from homeassistant.util import dt as dt_util

from .addon import async_ensure_addon_running
from .cart_builder import CartBuilder
from .const import ALL_CHAINS, CONF_WOOLWORTHS_STORE_ID, DOMAIN
from .coordinator import BasketBrainConfigEntry, BasketBrainCoordinator
from .foodstuffs import FoodstuffsCookieExpiredError
from .frontend import async_register_card
from .woolworths import CookieExpiredError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# How often to probe each chain's session. A live session is left alone; only
# a dead one is re-logged in. Cheap enough (one authed request per chain) to
# run through the day, unlike the old unconditional 03:00 browser re-login.
LOGIN_CHECK_INTERVAL = timedelta(hours=4)

_PIN_ALIAS_SCHEMA = vol.Schema(
    {
        vol.Required("phrase"): str,
        vol.Required("chain"): vol.In(["woolworths", "paknsave", "newworld"]),
        vol.Required("product_id"): str,
    }
)

_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("time"): vol.Match(r"^([01]\d|2[0-3]):[0-5]\d$"),
    }
)

_BUILD_CART_SCHEMA = vol.Schema(
    {
        vol.Required("chain"): vol.In(ALL_CHAINS),
    }
)

# chain is optional — omit it to re-login every active chain.
_RELOGIN_SCHEMA = vol.Schema(
    {
        vol.Optional("chain"): vol.In(ALL_CHAINS),
    }
)

_APPROVE_RESOLUTION_SCHEMA = vol.Schema(
    {
        vol.Required("phrase"): str,
        vol.Required("chain"): vol.In(ALL_CHAINS),
    }
)

_SET_QUANTITY_SCHEMA = vol.Schema(
    {
        vol.Required("phrase"): str,
        vol.Required("quantity"): vol.All(vol.Coerce(int), vol.Range(min=1, max=99)),
    }
)


def _staged_items(
    chain: str, resolved: dict, prices: dict, quantities: dict[str, int]
) -> tuple[list[dict], list[dict]]:
    """Split resolved items into what will be added vs skipped (out-of-stock).

    Mirrors the skip rule in CartBuilder.build_cart so the approval prompt
    shows exactly what will actually end up in the cart.
    """
    items: list[dict] = []
    out_of_stock: list[dict] = []

    for phrase, chain_ids in resolved.items():
        if not chain_ids.get(chain):
            continue
        priced = (prices.get(phrase) or {}).get(chain)
        if not priced:
            continue
        row = {
            "name": priced.get("name") or phrase,
            "price_nzd": priced.get("price_nzd"),
            "quantity": max(1, quantities.get(phrase, 1)),
        }
        if priced.get("in_stock", True):
            items.append(row)
        else:
            out_of_stock.append(row)

    return items, out_of_stock


def _load_woolworths_legacy_ids() -> dict[str, str]:
    """Old REST pickup addressId → GraphQL location id (blocking; run in executor).

    @returns mapping for the ids that changed
    """
    path = Path(__file__).parent / "stores" / "woolworths_legacy.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def async_migrate_entry(hass: HomeAssistant, entry: BasketBrainConfigEntry) -> bool:
    """Migrate config entries to the current version.

    @param hass: Home Assistant
    @param entry: entry to migrate
    @returns True when the entry is usable
    """
    if entry.version > 1:
        return False
    data = {**entry.data}
    if entry.minor_version < 3 and (old_id := data.get(CONF_WOOLWORTHS_STORE_ID)):
        legacy = await hass.async_add_executor_job(_load_woolworths_legacy_ids)
        if str(old_id) in legacy:
            data[CONF_WOOLWORTHS_STORE_ID] = legacy[str(old_id)]
            _LOGGER.info(
                "Woolworths store id %s migrated to %s", old_id, data[CONF_WOOLWORTHS_STORE_ID]
            )
    hass.config_entries.async_update_entry(entry, data=data, version=1, minor_version=3)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BasketBrainConfigEntry) -> bool:
    """Set up Basket Brain from a config entry."""
    if not is_hassio(hass):
        raise ConfigEntryError(
            "Basket Brain needs the Basket Brain Login add-on, which requires "
            "Home Assistant OS or Supervised. Container/Core installs are not "
            "supported."
        )

    await async_register_card(hass)

    try:
        addon_url, addon_token = await async_ensure_addon_running(hass)
    except AddonError as err:
        raise ConfigEntryNotReady(f"Login add-on not ready: {err}") from err

    coordinator = BasketBrainCoordinator(hass, entry, addon_url, addon_token)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    async def _on_shopping_list_updated(event) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(
        hass.bus.async_listen("shopping_list_updated", _on_shopping_list_updated)
    )

    # The bus event only fires for the legacy shopping_list layer — edits made
    # through the todo entity (To-do card, todo.* services) don't emit it, which
    # left list changes waiting for the next incidental refresh. The todo
    # entity's state is its pending-item count, so add/remove/complete all
    # change state and trigger here; async_request_refresh debounces the burst.
    async def _on_todo_changed(event) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(
        async_track_state_change_event(
            hass, ["todo.shopping_list"], _on_todo_changed
        )
    )

    async def _login_check(now) -> None:
        await coordinator.async_check_logins()

    entry.async_on_unload(
        async_track_time_interval(hass, _login_check, LOGIN_CHECK_INTERVAL)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    builder = CartBuilder()

    async def _do_build(data: dict, chain: str | None) -> None:
        """Stage a cart for one chain (explicit) or the cheapest (chain=None).

        Lands on sensor.pending_cart. Never places an order.
        """
        if not data:
            _LOGGER.warning("Basket Brain: no coordinator data yet — skipping build")
            return
        basket_totals = data.get("basket_totals") or {}
        resolved = data.get("resolved") or {}

        if chain is None:
            chain = await builder.pick_cheapest_chain(basket_totals)
            if chain is None:
                _LOGGER.warning("Basket Brain: no prices yet — nothing to stage")
                return

        total = basket_totals.get(chain)
        slot = await builder.get_next_slot(
            chain, coordinator.woolworths, coordinator.foodstuffs
        )

        prices = data.get("prices") or {}
        items, out_of_stock = _staged_items(
            chain, resolved, prices, coordinator.quantities
        )

        coordinator.set_pending_cart(
            {
                "chain": chain,
                "resolved": resolved,
                "prices": prices,
                "total_nzd": total,
                "items": items,
                "out_of_stock": out_of_stock,
                "next_slot": slot,
                "staged_at": dt_util.utcnow().isoformat(),
            }
        )

    async def handle_build_cheapest_cart(call: ServiceCall) -> None:
        await _do_build(coordinator.data or {}, None)

    async def handle_build_cart(call: ServiceCall) -> None:
        await _do_build(coordinator.data or {}, call.data["chain"])

    async def handle_approve_cart(call: ServiceCall) -> None:
        pending = coordinator.pending_cart
        if not pending:
            _LOGGER.warning("Basket Brain: no pending cart to approve")
            return

        async def _build() -> dict:
            return await builder.build_cart(
                pending["chain"],
                pending["resolved"],
                coordinator.woolworths,
                coordinator.foodstuffs,
                prices=pending.get("prices"),
                quantities=coordinator.quantities,
            )

        try:
            result = await _build()
        except (FoodstuffsCookieExpiredError, CookieExpiredError) as err:
            # Either chain's session died mid-build — re-login and retry once.
            # Woolworths uses CookieExpiredError (no banner); Foodstuffs carries
            # the banner on the error.
            chain = getattr(err, "banner", None) or pending["chain"]
            _LOGGER.info(
                "%s session expired during cart build — silent re-login", chain
            )
            if not await coordinator._retry_after_relogin(chain, err):
                _LOGGER.warning(
                    "%s login could not be refreshed — cart not built", chain
                )
                return
            try:
                result = await _build()
            except (FoodstuffsCookieExpiredError, CookieExpiredError):
                _LOGGER.warning("%s still failing after re-login", chain)
                return

        _LOGGER.info(
            "Basket Brain cart built on %s: "
            "added=%d compare_only=%d skipped=%d errors=%d",
            result["chain"],
            len(result["added"]),
            len(result["compare_only"]),
            len(result.get("skipped", [])),
            len(result["errors"]),
        )

        coordinator.set_pending_cart(None)

    async def handle_relogin(call: ServiceCall) -> None:
        """Force a fresh add-on login for one chain, or all active chains.

        Deterministic recovery lever: goes straight to the add-on and replaces
        the cookie jar, so it doesn't depend on a request happening to 401 on a
        path that surfaces the expiry.
        """
        chain = call.data.get("chain")
        chains = [chain] if chain else coordinator.active_chains
        for target in chains:
            try:
                ok = await coordinator.async_refresh_login(target)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Basket Brain: relogin for %s failed: %s", target, err)
                continue
            _LOGGER.info(
                "Basket Brain: relogin for %s %s", target,
                "succeeded" if ok else "rejected (reauth needed)",
            )
        await coordinator.async_request_refresh()

    async def handle_approve_resolution(call: ServiceCall) -> None:
        """Promote a low-confidence resolution to high and learn the phrase."""
        phrase = call.data["phrase"]
        chain = call.data["chain"]
        prices = (coordinator.data or {}).get("prices", {})
        entry = (prices.get(phrase) or {}).get(chain)
        if not entry:
            _LOGGER.warning(
                "approve_resolution: no price entry for %r on %s", phrase, chain
            )
            return
        product_id = entry.get("product_id")
        if not product_id:
            _LOGGER.warning(
                "approve_resolution: price entry for %r on %s has no product_id",
                phrase, chain,
            )
            return
        gtin = coordinator.product_map.find_gtin_by_product_id(chain, product_id)
        if gtin:
            coordinator.product_map.add_phrase(gtin, phrase)
            coordinator.product_map.promote_confidence(gtin, chain)
            await coordinator.product_map.async_save()
            _LOGGER.info(
                "Approved resolution: %r → product_id=%r on %s (gtin=%s)",
                phrase, product_id, chain, gtin,
            )
        else:
            # The resolution came from live search, not the map, so there's no
            # gtin to learn against — which is exactly why it was flagged low.
            # Pin the phrase→product_id alias instead: it persists to disk and
            # wins as the resolver's top tier at high confidence, so the item
            # stays approved across refreshes and restarts.
            coordinator.resolver.pin_alias(phrase, chain, product_id)
            await coordinator.resolver.async_save_aliases(hass)
            _LOGGER.info(
                "Approved resolution via alias pin: %r → product_id=%r on %s",
                phrase, product_id, chain,
            )
        # Update the in-memory price entry's confidence so the card reflects
        # the approval immediately without waiting for a full refresh.
        entry["confidence"] = "high"
        coordinator.async_update_listeners()

    async def handle_rebuild_map(call: ServiceCall) -> None:
        """Force an immediate rebuild of the barcode product map."""
        await coordinator.async_rebuild_map()

    async def handle_pin_alias(call: ServiceCall) -> None:
        coordinator.resolver.pin_alias(
            call.data["phrase"],
            call.data["chain"],
            call.data["product_id"],
        )
        await coordinator.resolver.async_save_aliases(hass)

    async def _scheduled_cb(now) -> None:
        await _do_build(coordinator.data or {}, None)

    async def handle_schedule_build(call: ServiceCall) -> None:
        time_str = call.data["time"]
        hour, minute = (int(part) for part in time_str.split(":"))
        if coordinator._schedule_unsub is not None:
            coordinator._schedule_unsub()
        coordinator._schedule_unsub = async_track_time_change(
            hass, _scheduled_cb, hour=hour, minute=minute, second=0
        )
        coordinator.schedule_time = time_str
        _LOGGER.info("Basket Brain scheduled daily build at %s", time_str)

    hass.services.async_register(DOMAIN, "rebuild_map", handle_rebuild_map)
    hass.services.async_register(
        DOMAIN, "approve_resolution", handle_approve_resolution,
        schema=_APPROVE_RESOLUTION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "pin_alias", handle_pin_alias, schema=_PIN_ALIAS_SCHEMA
    )

    async def handle_set_quantity(call: ServiceCall) -> None:
        await coordinator.async_set_quantity(
            call.data["phrase"], call.data["quantity"]
        )

    hass.services.async_register(
        DOMAIN, "set_quantity", handle_set_quantity, schema=_SET_QUANTITY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "build_cheapest_cart", handle_build_cheapest_cart
    )
    hass.services.async_register(
        DOMAIN, "build_cart", handle_build_cart, schema=_BUILD_CART_SCHEMA
    )
    hass.services.async_register(DOMAIN, "approve_cart", handle_approve_cart)
    hass.services.async_register(
        DOMAIN, "schedule_build", handle_schedule_build,
        schema=_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "relogin", handle_relogin, schema=_RELOGIN_SCHEMA
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BasketBrainConfigEntry
) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    if coordinator._schedule_unsub is not None:
        coordinator._schedule_unsub()
        coordinator._schedule_unsub = None
    if coordinator._rebuild_map_unsub is not None:
        coordinator._rebuild_map_unsub()
        coordinator._rebuild_map_unsub = None
    for service in (
        "rebuild_map", "approve_resolution", "pin_alias", "build_cheapest_cart",
        "build_cart", "approve_cart", "schedule_build", "relogin",
    ):
        hass.services.async_remove(DOMAIN, service)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
