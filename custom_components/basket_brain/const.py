from __future__ import annotations

from typing import Final

DOMAIN: Final = "basket_brain"

CONF_ENABLED_CHAINS: Final = "enabled_chains"
CONF_WOOLWORTHS_EMAIL: Final = "woolworths_email"
CONF_WOOLWORTHS_PASSWORD: Final = "woolworths_password"
CONF_FOODSTUFFS_EMAIL: Final = "foodstuffs_email"
CONF_FOODSTUFFS_PASSWORD: Final = "foodstuffs_password"

ADDON_NAME: Final = "Basket Brain Login"
ADDON_BASE_SLUG: Final = "basket_brain_login"
# "local_" prefix = installed from the local add-ons folder.
ADDON_SLUG: Final = f"local_{ADDON_BASE_SLUG}"
# The Supervisor derives a repository id from the URL: sha1(url)[:8]. This is
# that id for our repository, used only as a last-resort guess.
ADDON_FALLBACK_SLUG: Final = f"9020f6f4_{ADDON_BASE_SLUG}"
ADDON_PORT: Final = 8099
# Add-on option holding the shared secret between us and the add-on.
CONF_API_TOKEN: Final = "api_token"

CHAIN_WOOLWORTHS: Final = "woolworths"
CHAIN_PAKNSAVE: Final = "paknsave"
CHAIN_NEWWORLD: Final = "newworld"

ALL_CHAINS: Final = [CHAIN_WOOLWORTHS, CHAIN_PAKNSAVE, CHAIN_NEWWORLD]
FOODSTUFFS_CHAINS: Final = {CHAIN_PAKNSAVE, CHAIN_NEWWORLD}

CONF_WOOLWORTHS_STORE_ID: Final = "woolworths_store_id"
CONF_PAKNSAVE_STORE_ID: Final = "paknsave_store_id"
CONF_NEWWORLD_STORE_ID: Final = "newworld_store_id"

CONF_PRIMARY_CHAIN: Final = "primary_chain"

STORE_ID_KEYS: Final = {
    CHAIN_WOOLWORTHS: CONF_WOOLWORTHS_STORE_ID,
    CHAIN_PAKNSAVE: CONF_PAKNSAVE_STORE_ID,
    CHAIN_NEWWORLD: CONF_NEWWORLD_STORE_ID,
}
