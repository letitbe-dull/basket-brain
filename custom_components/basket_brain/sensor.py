from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ALL_CHAINS, CHAIN_NEWWORLD, CHAIN_PAKNSAVE, CHAIN_WOOLWORTHS
from .coordinator import BasketBrainConfigEntry, BasketBrainCoordinator
from .entity import BasketBrainEntity


@dataclass(frozen=True, kw_only=True)
class BasketBrainSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], float | int | None]


SENSORS: tuple[BasketBrainSensorDescription, ...] = (
    BasketBrainSensorDescription(
        key="basket_total_woolworths",
        translation_key="basket_total_woolworths",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        value_fn=lambda data: data.get("basket_totals", {}).get(CHAIN_WOOLWORTHS),
    ),
    BasketBrainSensorDescription(
        key="basket_total_paknsave",
        translation_key="basket_total_paknsave",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        value_fn=lambda data: data.get("basket_totals", {}).get(CHAIN_PAKNSAVE),
    ),
    BasketBrainSensorDescription(
        key="basket_total_newworld",
        translation_key="basket_total_newworld",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="NZD",
        suggested_display_precision=2,
        value_fn=lambda data: data.get("basket_totals", {}).get(CHAIN_NEWWORLD),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BasketBrainConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Basket Brain sensors."""
    coordinator = entry.runtime_data

    entities: list[SensorEntity] = [
        BasketBrainSensor(coordinator, description) for description in SENSORS
    ]
    entities.append(BasketBrainShoppingListSensor(coordinator))
    entities.append(BasketBrainPendingCartSensor(coordinator))
    entities.extend(
        BasketBrainChainItemsSensor(coordinator, chain) for chain in ALL_CHAINS
    )

    entities.append(BasketBrainSpecialsAlertsSensor(coordinator))

    async_add_entities(entities)


class BasketBrainSensor(BasketBrainEntity, SensorEntity):
    entity_description: BasketBrainSensorDescription

    def __init__(
        self,
        coordinator: BasketBrainCoordinator,
        description: BasketBrainSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self.coordinator.data or {})


class BasketBrainShoppingListSensor(BasketBrainEntity, SensorEntity):
    """One tile carrying the whole shopping list and each item's price per shop.

    The state is how many items are on the list; the full per-item, per-shop
    breakdown lives in the attributes so nothing has to be added or removed as
    the list changes.
    """

    _attr_translation_key = "shopping_list"

    def __init__(self, coordinator: BasketBrainCoordinator) -> None:
        super().__init__(coordinator, "shopping_list")

    @property
    def native_value(self) -> int:
        prices = (self.coordinator.data or {}).get("prices", {})
        return len(prices)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return per-item price, name, and confidence breakdown per shop."""
        data = self.coordinator.data or {}
        prices = data.get("prices", {})
        quantities = data.get("quantities", {})
        items = []
        for phrase, per_chain in prices.items():
            row: dict[str, Any] = {
                "item": phrase,
                "quantity": max(1, quantities.get(phrase, 1)),
            }
            for chain in ALL_CHAINS:
                entry = per_chain.get(chain)
                row[chain] = entry.get("price_nzd") if entry else None
                row[f"{chain}_name"] = entry.get("name") if entry else None
                row[f"{chain}_confidence"] = (
                    entry.get("confidence") if entry else None
                )
            items.append(row)
        return {
            "items": items,
            "primary_chain": data.get("primary_chain"),
            # Chains that dropped out of this update. The card uses this to
            # say so plainly instead of showing a silently empty row.
            "degraded_chains": data.get("degraded_chains", []),
        }


class BasketBrainPendingCartSensor(BasketBrainEntity, SensorEntity):
    """The cart that has been staged and is waiting for someone to approve it.

    Until now the staged cart only existed in a persistent notification, so a
    dashboard had no way to show it. The state is the shop the cart is staged
    at (or "none"), and everything needed to render an approval prompt — total,
    items, what was dropped for being out of stock — is in the attributes.
    """

    _attr_translation_key = "pending_cart"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["none", *ALL_CHAINS]

    def __init__(self, coordinator: BasketBrainCoordinator) -> None:
        super().__init__(coordinator, "pending_cart")

    @property
    def native_value(self) -> str:
        """Return the shop the cart is staged at, or "none"."""
        pending = self.coordinator.pending_cart
        return pending["chain"] if pending else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the staged cart, or empty values when nothing is pending."""
        pending = self.coordinator.pending_cart
        if not pending:
            return {
                "total_nzd": None,
                "item_count": 0,
                "items": [],
                "out_of_stock": [],
                "staged_at": None,
            }
        return {
            "total_nzd": pending.get("total_nzd"),
            "item_count": len(pending.get("items", [])),
            "items": pending.get("items", []),
            "out_of_stock": pending.get("out_of_stock", []),
            "staged_at": pending.get("staged_at"),
        }


class BasketBrainChainItemsSensor(BasketBrainEntity, SensorEntity):
    """One tile per shop carrying its full priced item list.

    The state is how many list items were found (and priced) at that shop;
    the name + price for every item lives in the attributes, so a dashboard
    card (e.g. auto-entities or markdown) can render the whole basket for
    that one shop without any templating against the other shops' data.
    """

    def __init__(self, coordinator: BasketBrainCoordinator, chain: str) -> None:
        super().__init__(coordinator, f"items_{chain}")
        self._chain = chain
        self._attr_translation_key = f"items_{chain}"

    def _chain_items(self) -> list[dict[str, Any]]:
        """Return {name, price_nzd, confidence} for every priced list item.

        The card (Phase 5) needs `confidence` alongside the name to flag
        anything that came from a low-confidence search fallback.
        """
        data = self.coordinator.data or {}
        prices = data.get("prices", {})
        quantities = data.get("quantities", {})
        items = []
        for phrase, per_chain in prices.items():
            entry = per_chain.get(self._chain)
            if entry and entry.get("price_nzd") is not None:
                items.append({
                    "phrase": phrase,
                    "name": entry.get("name") or phrase,
                    "price_nzd": entry["price_nzd"],
                    "quantity": max(1, quantities.get(phrase, 1)),
                    "confidence": entry.get("confidence"),
                })
        return items

    @property
    def native_value(self) -> int:
        return len(self._chain_items())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"items": self._chain_items()}


class BasketBrainSpecialsAlertsSensor(BasketBrainEntity, SensorEntity):
    """Count of the user's usual products that are currently on special.

    State is the number of usuals on special across all enabled chains.
    Attributes carry the full list so the card can render the specials face.
    """

    _attr_translation_key = "specials_alerts"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BasketBrainCoordinator) -> None:
        super().__init__(coordinator, "specials_alerts")

    def _alerts(self) -> list[dict[str, Any]]:
        return (self.coordinator.data or {}).get("specials_alerts", [])

    @property
    def native_value(self) -> int:
        return len(self._alerts())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"items": self._alerts()}
