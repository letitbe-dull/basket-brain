from __future__ import annotations

import json
import logging
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.hassio import AddonError
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio

from .addon import async_ensure_addon_running
from .const import (
    ALL_CHAINS,
    CHAIN_PAKNSAVE,
    CHAIN_WOOLWORTHS,
    CONF_ENABLED_CHAINS,
    CONF_FOODSTUFFS_EMAIL,
    CONF_FOODSTUFFS_PASSWORD,
    CONF_PRIMARY_CHAIN,
    CONF_WOOLWORTHS_EMAIL,
    CONF_WOOLWORTHS_PASSWORD,
    DOMAIN,
    FOODSTUFFS_CHAINS,
    STORE_ID_KEYS,
)
from .login_client import InvalidCredentialsError, LoginClient, LoginError

_LOGGER = logging.getLogger(__name__)

_STORES_DIR = Path(__file__).parent / "stores"


def _load_bundled_stores(chain: str) -> list[dict[str, Any]]:
    """Load the bundled store list for a chain (blocking — run in executor)."""
    path = _STORES_DIR / f"{chain}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 6371 * 2 * atan2(sqrt(a), sqrt(1 - a))


def _sort_by_distance(
    stores: list[dict[str, Any]], home_lat: float, home_lon: float
) -> list[dict[str, Any]]:
    """Nearest-first when stores carry coordinates; untouched otherwise."""
    if not any(s.get("lat") is not None for s in stores):
        return stores
    return sorted(
        stores,
        key=lambda s: (
            _distance_km(home_lat, home_lon, s["lat"], s["lon"])
            if s.get("lat") is not None and s.get("lon") is not None
            else float("inf")
        ),
    )


def _store_label(store: dict[str, Any]) -> str:
    name = store.get("name") or store.get("id") or "Unknown store"
    address = store.get("address") or store.get("storeAddress")
    return f"{name} — {address}" if address else str(name)


def _credentials_schema(email_key: str, password_key: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(email_key): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
            ),
            vol.Required(password_key): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


class BasketBrainConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select which chains to enable."""
        if not is_hassio(self.hass):
            return self.async_abort(reason="no_supervisor")

        if user_input is not None:
            self._data[CONF_ENABLED_CHAINS] = user_input[CONF_ENABLED_CHAINS]
            return await self.async_step_stores()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED_CHAINS,
                        default=[],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=ALL_CHAINS,
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key=CONF_ENABLED_CHAINS,
                        )
                    ),
                }
            ),
        )

    async def _store_selector(self, chain: str) -> selector.Selector:
        """Build a searchable store dropdown from the bundled store list."""
        try:
            stores = await self.hass.async_add_executor_job(
                _load_bundled_stores, chain
            )
            stores = _sort_by_distance(
                stores, self.hass.config.latitude, self.hass.config.longitude
            )
            sorted_by_distance = any(s.get("lat") is not None for s in stores)
            options = [
                selector.SelectOptionDict(value=str(s["id"]), label=_store_label(s))
                for s in stores
                if s.get("id")
            ]
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Bundled store list missing for %s; using free text", chain)
            options = []

        if options:
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    # Nearest-first when we have coordinates; alphabetical
                    # otherwise (sort=True would clobber the distance order).
                    sort=not sorted_by_distance,
                )
            )
        return selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )

    async def async_step_stores(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a store for every enabled chain, all on one screen."""
        chosen = self._data.get(CONF_ENABLED_CHAINS, [])
        enabled = [c for c in ALL_CHAINS if c in chosen]

        if user_input is not None:
            for chain in enabled:
                key = STORE_ID_KEYS[chain]
                if user_input.get(key):
                    self._data[key] = user_input[key]
            return await self.async_step_primary_chain()

        schema = {
            vol.Required(STORE_ID_KEYS[chain]): await self._store_selector(chain)
            for chain in enabled
        }
        return self.async_show_form(
            step_id="stores", data_schema=vol.Schema(schema)
        )

    async def async_step_primary_chain(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the chain that seeds purchase history/frequency.

        Only one chain enabled → nothing to choose, so set it and move on.
        The default is the current auto-pick order (first enabled chain in
        ALL_CHAINS order), so clicking straight through changes nothing.
        """
        enabled = [
            c for c in ALL_CHAINS if c in self._data.get(CONF_ENABLED_CHAINS, [])
        ]

        if len(enabled) <= 1:
            if enabled:
                self._data[CONF_PRIMARY_CHAIN] = enabled[0]
            return await self._continue_to_auth()

        if user_input is not None:
            self._data[CONF_PRIMARY_CHAIN] = user_input[CONF_PRIMARY_CHAIN]
            return await self._continue_to_auth()

        return self.async_show_form(
            step_id="primary_chain",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PRIMARY_CHAIN, default=enabled[0]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=enabled,
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key=CONF_ENABLED_CHAINS,
                        )
                    ),
                }
            ),
        )

    async def _continue_to_auth(self) -> ConfigFlowResult:
        """After stores, collect credentials for each enabled shop."""
        enabled = self._data.get(CONF_ENABLED_CHAINS, [])
        if FOODSTUFFS_CHAINS.intersection(enabled):
            return await self.async_step_foodstuffs_auth()
        if CHAIN_WOOLWORTHS in enabled:
            return await self.async_step_woolworths_auth()
        return self.async_create_entry(title="Basket Brain", data=self._data)

    async def _verify_login(
        self, shop: str, email: str, password: str
    ) -> str | None:
        """Ask the add-on to try a login. Returns None on success, else error key.

        Installs and starts the login add-on first if needed (one-click
        install — same pattern as Z-Wave JS).
        """
        try:
            base_url, api_token = await async_ensure_addon_running(self.hass)
        except AddonError as err:
            _LOGGER.warning("Login add-on could not be started: %s", err)
            return "addon_failed"

        client = LoginClient(
            async_get_clientsession(self.hass), base_url, api_token
        )
        try:
            await client.login(shop, email, password)
        except InvalidCredentialsError:
            return "invalid_auth"
        except LoginError as err:
            _LOGGER.warning("Login add-on unreachable during setup: %s", err)
            return "cannot_connect"
        return None

    async def async_step_foodstuffs_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect Foodstuffs (Club+) email + password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_FOODSTUFFS_EMAIL]
            password = user_input[CONF_FOODSTUFFS_PASSWORD]
            # Pick either enabled banner for the verification call — Club+ is
            # shared across banners.
            enabled = self._data.get(CONF_ENABLED_CHAINS, [])
            shop = CHAIN_PAKNSAVE if CHAIN_PAKNSAVE in enabled else next(
                iter(FOODSTUFFS_CHAINS.intersection(enabled))
            )
            err = await self._verify_login(shop, email, password)
            if err:
                errors["base"] = err
            else:
                self._data[CONF_FOODSTUFFS_EMAIL] = email
                self._data[CONF_FOODSTUFFS_PASSWORD] = password
                if CHAIN_WOOLWORTHS in enabled:
                    return await self.async_step_woolworths_auth()
                return self.async_create_entry(title="Basket Brain", data=self._data)

        return self.async_show_form(
            step_id="foodstuffs_auth",
            data_schema=_credentials_schema(
                CONF_FOODSTUFFS_EMAIL, CONF_FOODSTUFFS_PASSWORD
            ),
            errors=errors,
        )

    async def async_step_woolworths_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_WOOLWORTHS_EMAIL]
            password = user_input[CONF_WOOLWORTHS_PASSWORD]
            err = await self._verify_login(CHAIN_WOOLWORTHS, email, password)
            if err:
                errors["base"] = err
            else:
                self._data[CONF_WOOLWORTHS_EMAIL] = email
                self._data[CONF_WOOLWORTHS_PASSWORD] = password
                return self.async_create_entry(title="Basket Brain", data=self._data)

        return self.async_show_form(
            step_id="woolworths_auth",
            data_schema=_credentials_schema(
                CONF_WOOLWORTHS_EMAIL, CONF_WOOLWORTHS_PASSWORD
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Route reauth to whichever shop's login was rejected."""
        chain = self.context.get("chain", CHAIN_WOOLWORTHS)
        if chain in FOODSTUFFS_CHAINS:
            return await self.async_step_reauth_foodstuffs()
        return await self.async_step_reauth_confirm()

    async def _do_reauth(
        self,
        step_id: str,
        shop: str,
        email_key: str,
        password_key: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Shared reauth handler: re-enter credentials for one shop."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[email_key]
            password = user_input[password_key]
            err = await self._verify_login(shop, email, password)
            if err:
                errors["base"] = err
            else:
                entry = self._get_reauth_entry()
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, email_key: email, password_key: password},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id=step_id,
            data_schema=_credentials_schema(email_key, password_key),
            errors=errors,
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._do_reauth(
            "reauth_confirm",
            CHAIN_WOOLWORTHS,
            CONF_WOOLWORTHS_EMAIL,
            CONF_WOOLWORTHS_PASSWORD,
            user_input,
        )

    async def async_step_reauth_foodstuffs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._do_reauth(
            "reauth_foodstuffs",
            CHAIN_PAKNSAVE,
            CONF_FOODSTUFFS_EMAIL,
            CONF_FOODSTUFFS_PASSWORD,
            user_input,
        )
