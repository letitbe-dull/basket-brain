from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import BasketBrainCoordinator


class BasketBrainEntity(CoordinatorEntity[BasketBrainCoordinator]):
    """Base class for all Basket Brain entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BasketBrainCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
