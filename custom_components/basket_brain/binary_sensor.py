"""Binary sensor platform — one "signed in" sensor per enabled chain."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import BasketBrainConfigEntry, BasketBrainCoordinator
from .entity import BasketBrainEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BasketBrainConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one login sensor per active chain."""
    coordinator = entry.runtime_data
    async_add_entities(
        BasketBrainLoginSensor(coordinator, chain)
        for chain in coordinator.active_chains
    )


class BasketBrainLoginSensor(BasketBrainEntity, BinarySensorEntity):
    """Whether we hold a live session for one chain."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: BasketBrainCoordinator, chain: str) -> None:
        super().__init__(coordinator, f"login_{chain}")
        self._chain = chain
        self._attr_translation_key = f"login_{chain}"

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_signed_in(self._chain)

    @property
    def available(self) -> bool:
        """Always available — state reads the saved jar, not the last fetch.

        A failed fetch cycle shouldn't blank out login status for chains
        that are actually fine.
        """
        return True
