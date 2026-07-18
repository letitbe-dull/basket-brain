"""Manage the Basket Brain Login add-on via the Supervisor."""

from __future__ import annotations

import asyncio
import logging
import secrets

from aiohasupervisor import SupervisorError
from aiohttp import ClientError, ClientTimeout
from homeassistant.components.hassio import (
    AddonError,
    AddonInfo,
    AddonManager,
    AddonState,
)
from homeassistant.components.hassio.handler import get_supervisor_client
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.singleton import singleton

from .const import (
    ADDON_BASE_SLUG,
    ADDON_FALLBACK_SLUG,
    ADDON_NAME,
    ADDON_PORT,
    ADDON_SLUG,
    CONF_API_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DATA_ADDON_MANAGER = f"{DOMAIN}_addon_manager"
DATA_ADDON_SLUG = f"{DOMAIN}_addon_slug"


def _is_login_addon(slug: str) -> bool:
    """Match the login add-on under any repository prefix."""
    return slug == ADDON_SLUG or slug.endswith(f"_{ADDON_BASE_SLUG}")


async def async_resolve_addon_slug(hass: HomeAssistant) -> str:
    """Resolve the slug of the Basket Brain Login add-on dynamically.

    The Supervisor prefixes an add-on's slug with an id derived from the
    repository it came from, so the full slug differs per install. Only a
    real match is cached — a fallback must stay retryable, otherwise a user
    who adds the add-on repository after a failed setup stays broken until
    they restart Home Assistant.
    """
    if (resolved_slug := hass.data.get(DATA_ADDON_SLUG)) is not None:
        return resolved_slug

    supervisor = get_supervisor_client(hass)

    try:
        for addon in await supervisor.addons.list():
            if _is_login_addon(addon.slug):
                _LOGGER.debug("Found installed login add-on: %s", addon.slug)
                hass.data[DATA_ADDON_SLUG] = addon.slug
                return addon.slug
    except SupervisorError as err:
        _LOGGER.warning("Failed to list installed add-ons: %s", err)

    try:
        for addon in await supervisor.store.addons_list():
            if _is_login_addon(addon.slug):
                _LOGGER.debug("Found login add-on in store: %s", addon.slug)
                hass.data[DATA_ADDON_SLUG] = addon.slug
                return addon.slug
    except SupervisorError as err:
        _LOGGER.warning("Failed to list store add-ons: %s", err)

    # Not found — guess the slug for our own repository URL, but do not cache
    # it, so a later retry re-checks the Supervisor.
    _LOGGER.debug("Login add-on not found, falling back to %s", ADDON_FALLBACK_SLUG)
    return ADDON_FALLBACK_SLUG


@singleton(DATA_ADDON_MANAGER)
@callback
def get_addon_manager(hass: HomeAssistant) -> AddonManager:
    """Return the shared add-on manager. Callers set the slug they resolved."""
    return AddonManager(hass, _LOGGER, ADDON_NAME, ADDON_SLUG)


async def _async_ensure_api_token(manager: AddonManager, info: AddonInfo) -> str:
    """Return the add-on's shared secret, generating one on first run.

    Every add-on on the Supervisor network can reach the login port, so the
    add-on only accepts requests carrying this token. The add-on options are
    the single source of truth — nothing is stored on our side.
    """
    if token := (info.options or {}).get(CONF_API_TOKEN):
        return token

    _LOGGER.info("Generating an API token for the login add-on")
    token = secrets.token_urlsafe(32)
    await manager.async_set_addon_options(
        {**(info.options or {}), CONF_API_TOKEN: token}
    )
    return token


async def async_ensure_addon_running(hass: HomeAssistant) -> tuple[str, str]:
    """Make sure the login add-on is installed, configured and running.

    Returns its base URL on the Supervisor network (e.g.
    ``http://local-basket-brain-login:8099``) and the shared API token.

    Raises AddonError when the Supervisor refuses or the add-on cannot be
    brought up — callers translate that into ConfigEntryNotReady or a
    config-flow error.
    """
    slug = await async_resolve_addon_slug(hass)
    manager = get_addon_manager(hass)
    manager.addon_slug = slug
    info = await manager.async_get_addon_info()

    if info.state == AddonState.NOT_INSTALLED:
        _LOGGER.info("Login add-on not installed — installing")
        await manager.async_install_addon()
        info = await manager.async_get_addon_info()

    if info.state in (AddonState.INSTALLING, AddonState.UPDATING):
        raise AddonError(f"{ADDON_NAME} add-on is busy ({info.state})")

    had_token = bool((info.options or {}).get(CONF_API_TOKEN))
    token = await _async_ensure_api_token(manager, info)

    if info.state == AddonState.NOT_RUNNING:
        _LOGGER.info("Login add-on not running — starting")
        await manager.async_start_addon()
        info = await manager.async_get_addon_info()
    elif not had_token:
        # The token is read at startup, so a running add-on needs a restart
        # before it will accept it.
        _LOGGER.info("Restarting login add-on to pick up its new API token")
        await manager.async_restart_addon()
        info = await manager.async_get_addon_info()

    if info.state != AddonState.RUNNING or not info.hostname:
        raise AddonError(
            f"{ADDON_NAME} add-on failed to start (state: {info.state})"
        )

    base_url = f"http://{info.hostname}:{ADDON_PORT}"
    # "Running" per the Supervisor only means the container started — the
    # HTTP server inside needs a moment to bind. Wait for /health.
    await _async_wait_until_ready(hass, base_url)
    return base_url, token


async def _async_wait_until_ready(
    hass: HomeAssistant, base_url: str, deadline_seconds: float = 60
) -> None:
    """Poll the add-on's /health endpoint until it answers."""
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(deadline_seconds):
            while True:
                try:
                    async with session.get(
                        f"{base_url}/health", timeout=ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 200:
                            return
                except (ClientError, TimeoutError):
                    pass
                await asyncio.sleep(2)
    except TimeoutError as err:
        raise AddonError(
            f"{ADDON_NAME} add-on did not become reachable at {base_url}"
        ) from err
