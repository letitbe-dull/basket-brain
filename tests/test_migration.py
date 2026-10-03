"""Config entry 1.2 → 1.3: Woolworths REST store ids become GraphQL location ids."""

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.basket_brain import async_migrate_entry
from custom_components.basket_brain.const import CONF_WOOLWORTHS_STORE_ID, DOMAIN


@pytest.mark.parametrize(
    ("old_id", "new_id"),
    [
        ("1225718", "9174"),  # Woolworths Northlands: store addressId → storeId
        ("3873690", "3873690"),  # EXPRESS PU Spotswood: pickup point keeps its id
        ("1906035", "1906035"),  # Quay Street: no current location, left for a re-pick
    ],
)
async def test_woolworths_store_id_migrates(
    hass: HomeAssistant, old_id: str, new_id: str
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={CONF_WOOLWORTHS_STORE_ID: old_id},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.data[CONF_WOOLWORTHS_STORE_ID] == new_id
    assert entry.minor_version == 3


async def test_entry_without_woolworths_store_still_bumps(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2, data={})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.data == {}
    assert entry.minor_version == 3
