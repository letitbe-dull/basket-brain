from __future__ import annotations

import logging
from typing import Any

from .const import CHAIN_WOOLWORTHS
from .foodstuffs import FoodstuffsClient, FoodstuffsCookieExpiredError
from .woolworths import CookieExpiredError, WoolworthsClient

_LOGGER = logging.getLogger(__name__)


class CartBuilder:
    """Pick cheapest chain, build cart, get next slot."""

    async def pick_cheapest_chain(
        self, basket_totals: dict[str, float | None]
    ) -> str | None:
        """Return chain with the lowest non-None basket total, or None."""
        priced = {c: t for c, t in basket_totals.items() if t is not None}
        if not priced:
            return None
        return min(priced, key=priced.get)

    async def build_cart(
        self,
        chain: str,
        resolved: dict,
        woolworths_client: WoolworthsClient | None,
        foodstuffs_clients: dict[str, FoodstuffsClient],
        prices: dict | None = None,
        quantities: dict[str, int] | None = None,
    ) -> dict:
        """Add resolved items to the chain's cart. Returns a summary dict.

        `quantities` maps phrase → wanted count; absent phrases default to 1.
        """
        added: list[dict] = []
        compare_only: list[dict] = []
        skipped: list[dict] = []
        errors: list[dict] = []

        if chain == CHAIN_WOOLWORTHS:
            client = woolworths_client
        else:
            client = foodstuffs_clients.get(chain)

        if client is None:
            errors.append({"phrase": None, "error": f"no client for chain {chain}"})
            return {
                "chain": chain,
                "added": added,
                "compare_only": compare_only,
                "skipped": skipped,
                "errors": errors,
            }

        for phrase, chain_ids in resolved.items():
            resolution = chain_ids.get(chain)
            if not resolution:
                continue
            product_id = resolution["product_id"]

            if prices is not None:
                chain_entry = (prices.get(phrase) or {}).get(chain)
                if chain_entry is not None and not chain_entry.get("in_stock", True):
                    _LOGGER.info(
                        "Skipping out-of-stock item %s (%s) on %s",
                        phrase, product_id, chain,
                    )
                    skipped.append({
                        "phrase": phrase,
                        "product_id": product_id,
                        "reason": "out_of_stock",
                    })
                    continue

            qty = max(1, (quantities or {}).get(phrase, 1))
            try:
                await client.add_to_cart(product_id, quantity=qty)
                added.append({
                    "phrase": phrase, "product_id": product_id, "quantity": qty,
                })
            except (FoodstuffsCookieExpiredError, CookieExpiredError):
                # Auth dead for the whole chain — let caller trigger re-login.
                # Woolworths raises CookieExpiredError; without this it fell into
                # the generic handler below and never re-logged in.
                raise
            except NotImplementedError:
                compare_only.append({"phrase": phrase, "product_id": product_id})
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "add_to_cart failed for %s (%s): %s", phrase, product_id, err
                )
                errors.append(
                    {"phrase": phrase, "product_id": product_id, "error": str(err)}
                )

        return {
            "chain": chain,
            "added": added,
            "compare_only": compare_only,
            "skipped": skipped,
            "errors": errors,
        }

    async def get_next_slot(
        self,
        chain: str,
        woolworths_client: WoolworthsClient | None,
        foodstuffs_clients: dict[str, FoodstuffsClient],
    ) -> str | None:
        """Return a human-readable next-available slot, or None."""
        if chain == CHAIN_WOOLWORTHS:
            if woolworths_client is None:
                return None
            try:
                slots = await woolworths_client.get_timeslots()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Woolworths get_timeslots failed: %s", err)
                return None
            return _first_slot_string(slots)

        client = foodstuffs_clients.get(chain)
        if client is None:
            return None
        try:
            return await client.get_next_slot()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Foodstuffs get_next_slot failed for %s: %s", chain, err)
            return None


def _first_slot_string(slots: Any) -> str | None:
    """Best-effort extraction of the first available slot as a string."""
    if not slots:
        return None
    if isinstance(slots, list):
        for entry in slots:
            s = _slot_to_string(entry)
            if s:
                return s
        return None
    if isinstance(slots, dict):
        for key in ("slots", "timeslots", "available", "data"):
            if key in slots:
                return _first_slot_string(slots[key])
        return _slot_to_string(slots)
    return str(slots)


def _slot_to_string(entry: Any) -> str | None:
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        if entry.get("available") is False or entry.get("isAvailable") is False:
            return None
        for k in ("displayName", "label", "startTime", "start", "time", "date"):
            if entry.get(k):
                return str(entry[k])
        return None
    return str(entry)
