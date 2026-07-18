from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    CONF_WOOLWORTHS_STORE_ID,
    DOMAIN,
    FOODSTUFFS_CHAINS,
    STORE_ID_KEYS,
)
from .cookie_store import CookieJar, CookieStore
from .foodstuffs import FoodstuffsClient, FoodstuffsCookieExpiredError
from .login_client import InvalidCredentialsError, LoginClient, LoginError
from .product_map import ProductMap
from .resolver import ShoppingListManager
from .woolworths import CookieExpiredError, WoolworthsClient, present_name

_LOGGER = logging.getLogger(__name__)

# Minimum gap between browser logins for one chain. Guards against a re-login
# storm: a fresh Club+ login invalidates the previous session, so hammering
# logins logs the account out over and over.
_RELOGIN_COOLDOWN_S = 90

# Foodstuffs tokens idle-expire ~30 min in. Proactively refreshing a bit
# before that (via the same get-current-user call the client already makes)
# means a fetch after a quiet gap doesn't have to 401 first.
_FOODSTUFFS_TOKEN_MAX_AGE_S = 25 * 60

type BasketBrainConfigEntry = ConfigEntry[BasketBrainCoordinator]


class BasketBrainCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: BasketBrainConfigEntry,
        addon_url: str,
        addon_token: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=24),
        )
        self.entry = entry

        self.login_client = LoginClient(
            async_get_clientsession(hass), addon_url, addon_token
        )

        self.woolworths: WoolworthsClient | None = None
        self.foodstuffs: dict[str, FoodstuffsClient] = {}

        enabled = entry.data.get(CONF_ENABLED_CHAINS, [])
        if CHAIN_WOOLWORTHS in enabled:
            store_id = entry.data.get(CONF_WOOLWORTHS_STORE_ID) or None
            self.woolworths = WoolworthsClient({}, store_id=store_id)
            self._wire_cookie_rotation(CHAIN_WOOLWORTHS, self.woolworths)

        enabled_foodstuffs = FOODSTUFFS_CHAINS.intersection(enabled)
        for banner in enabled_foodstuffs:
            store_id = entry.data.get(STORE_ID_KEYS[banner], "")
            if store_id:
                client = FoodstuffsClient(banner, store_id, cookies={})
                self._wire_cookie_rotation(banner, client)
                self.foodstuffs[banner] = client
            else:
                _LOGGER.warning(
                    "Foodstuffs banner %r enabled but no store selected — skipping",
                    banner,
                )

        self.product_map = ProductMap(hass, entry.entry_id)
        self.resolver = ShoppingListManager(product_map=self.product_map)

        self.pending_cart: dict | None = None
        self.schedule_time: str | None = None
        self._schedule_unsub = None
        self._rebuild_map_unsub = None
        self._cookies = CookieStore(hass, entry.entry_id)

        # Per-phrase quantities (default 1, only >1 stored). Lives here, not
        # in the todo list, so item text stays clean for the resolver.
        self.quantities: dict[str, int] = {}
        self._qty_store: Store[dict[str, int]] = Store(
            hass, 1, f"{DOMAIN}.quantities.{entry.entry_id}"
        )
        self._qty_loaded = False

        # Re-login throttle: one lock + one cooldown deadline per chain.
        self._login_locks: dict[str, asyncio.Lock] = {}
        self._login_cooldown_until: dict[str, float] = {}
        # Chains that gave up this cycle. Held out of the fetch so the
        # surviving chains still produce data; cleared at the start of the
        # next tick so a chain gets a clean go every time.
        self._degraded_chains: set[str] = set()

        # When a Foodstuffs chain's token was last confirmed fresh — drives
        # the proactive refresh in _async_update_data.
        self._foodstuffs_token_refreshed_at: dict[str, float] = {}

    def _wire_cookie_rotation(
        self, chain: str, client: FoodstuffsClient | WoolworthsClient
    ) -> None:
        """Persist a chain's jar whenever the shop rotates session cookies.

        Both chains rotate cookies via Set-Cookie during normal use; a jar
        frozen at login time goes stale (Foodstuffs ~30 min, Woolworths on
        its strict /shoppers/my endpoints). Saving each rotation keeps the
        persisted jar live, so restarts resume the session instead of
        costing a browser login.
        """

        def _on_rotated(domain: str, cookies: dict[str, str]) -> None:
            self.hass.async_create_task(
                self._async_save_rotated(chain, domain, cookies)
            )

        client.on_cookies_rotated = _on_rotated

    async def _async_save_rotated(
        self, chain: str, domain: str, cookies: dict[str, str]
    ) -> None:
        saved = self._cookies.get(chain)
        if saved is None:
            # Jar was dropped (dead session) — don't resurrect it.
            return
        await self._cookies.async_set(chain, {**saved, domain: cookies})

    def _credentials_for(self, chain: str) -> tuple[str, str] | None:
        """Return (email, password) for a chain, or None if not configured."""
        if chain == CHAIN_WOOLWORTHS:
            email = self.entry.data.get(CONF_WOOLWORTHS_EMAIL)
            password = self.entry.data.get(CONF_WOOLWORTHS_PASSWORD)
        else:
            email = self.entry.data.get(CONF_FOODSTUFFS_EMAIL)
            password = self.entry.data.get(CONF_FOODSTUFFS_PASSWORD)
        if not email or not password:
            return None
        return email, password

    async def async_refresh_login(self, chain: str) -> bool:
        """Ask the add-on for a fresh cookie jar for one chain.

        Throttled: a per-chain lock serialises logins so concurrent 401s can't
        each spawn a browser, and a cooldown stops a chain re-logging in more
        than once per window. Without this, expiries on many paths stormed the
        add-on — and each fresh Club+ login logged the previous session out.

        Returns True on success, False when creds are wrong / add-on refuses.
        Raises LoginError on transport failures — callers decide whether to
        surface those as UpdateFailed or swallow.
        """
        lock = self._login_locks.setdefault(chain, asyncio.Lock())
        async with lock:
            now = self.hass.loop.time()
            if now < self._login_cooldown_until.get(chain, 0.0):
                _LOGGER.debug(
                    "Skipping %s re-login — one was attempted < %ds ago",
                    chain, _RELOGIN_COOLDOWN_S,
                )
                # Report whatever state that recent attempt left us in.
                return self.is_signed_in(chain)
            self._login_cooldown_until[chain] = now + _RELOGIN_COOLDOWN_S
            return await self._perform_refresh_login(chain)

    async def _perform_refresh_login(self, chain: str) -> bool:
        """The actual add-on login. Only called under the per-chain lock."""
        creds = self._credentials_for(chain)
        if not creds:
            _LOGGER.warning("No credentials stored for %s — cannot refresh", chain)
            return False
        email, password = creds
        try:
            cookies = await self.login_client.login(chain, email, password)
        except InvalidCredentialsError as err:
            _LOGGER.warning("Add-on rejected %s credentials: %s", chain, err)
            # The jar (if any) is dead — drop it so the login sensor stops
            # claiming we're signed in while reauth is pending.
            # Drop the dead jar so the login sensor reflects reauth is pending.
            await self._cookies.async_drop(chain)
            self.async_update_listeners()
            return False

        await self._apply_cookies(chain, cookies)
        await self._cookies.async_set(chain, cookies)
        _LOGGER.info("Refreshed login for %s", chain)
        self.async_update_listeners()
        return True

    async def _apply_cookies(self, chain: str, cookies: CookieJar) -> None:
        """Push a cookie jar into a chain's client and bind its store.

        Raises CookieExpiredError / FoodstuffsCookieExpiredError if the shop
        rejects the jar — that's how a restored-but-dead jar gets caught.
        Other set_store failures are only a warning: prices may be from the
        wrong store, but the session itself is fine.
        """
        if chain == CHAIN_WOOLWORTHS and self.woolworths:
            client = self.woolworths
            client.update_cookies(cookies)
            if not client.store_id:
                _LOGGER.debug("Woolworths: no store configured, skipping set_store")
                return
        elif chain in self.foodstuffs:
            client = self.foodstuffs[chain]
            client.update_cookies(cookies)
        else:
            return

        try:
            await client.set_store()
        except (CookieExpiredError, FoodstuffsCookieExpiredError):
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s set_store failed (prices may be wrong): %s", chain, err)

        if chain in self.foodstuffs:
            self._foodstuffs_token_refreshed_at[chain] = self.hass.loop.time()

    async def _restore_or_login(self, chain: str) -> None:
        """Reuse a saved cookie jar if we have a fresh one, else log in.

        This is what keeps a Home Assistant restart from costing ~30s of
        headless browser per chain.
        """
        saved = self._cookies.get(chain)
        if saved is None:
            await self._require_login(chain)
            return

        _LOGGER.debug("Reusing saved cookies for %s — no browser login needed", chain)
        try:
            await self._apply_cookies(chain, saved)
        except (CookieExpiredError, FoodstuffsCookieExpiredError):
            _LOGGER.info("Saved cookies for %s are dead — logging in again", chain)
            await self._cookies.async_drop(chain)
            await self._require_login(chain)

    def _primary_chain(self) -> str | None:
        """Return the primary shopping chain for history seeding.

        Uses CONF_PRIMARY_CHAIN if set; otherwise defaults to the first
        enabled chain in preference order (Woolworths → PAK'nSAVE → New World).
        """
        clients = self._build_clients()
        explicit = self.entry.data.get(CONF_PRIMARY_CHAIN)
        if explicit and explicit in clients:
            return explicit
        for chain in ALL_CHAINS:
            if chain in clients:
                return chain
        return None

    async def _seed_product_map(self) -> None:
        """Seed the barcode map from primary-chain purchase history.

        Background task."""
        primary = self._primary_chain()
        if not primary:
            _LOGGER.warning("ProductMap: no enabled chain to seed from")
            return
        clients = self._build_clients()
        primary_client = clients.get(primary)
        if not primary_client:
            return
        await self.product_map.seed_from_history(primary, primary_client, clients)

    async def _async_setup(self) -> None:
        """Warm resolver caches, seed cookies, and load persisted aliases."""
        await self.resolver.async_load_aliases(self.hass)
        await self._cookies.async_load()
        await self.product_map.async_load()

        # A chain that can't log in here must not stop the integration from
        # loading. Raising out of _async_setup becomes ConfigEntryNotReady,
        # which costs every entity — including the chains that were fine.
        # Degrade the bad chain and carry on; the next tick retries it.
        for chain in self._build_clients():
            try:
                await self._restore_or_login(chain)
            except UpdateFailed as err:
                self._degrade(chain, f"could not log in at startup: {err}")

        # Seed the barcode map in the background so it doesn't delay startup.
        # The map supersedes the old per-chain "usuals" cache — no separate
        # load_usual step is needed here any more.
        self.hass.async_create_task(self._seed_product_map())

        # Weekly map rebuild — keeps the map current as purchase history drifts.
        # _seed_product_map is idempotent: new items are added, existing entries
        # (and their learned phrases[]) are preserved/updated, removed items
        # retain their entries (they may still be in the map from prior buys).
        self._rebuild_map_unsub = async_track_time_interval(
            self.hass,
            self._async_weekly_rebuild,
            timedelta(weeks=1),
        )

    async def _async_weekly_rebuild(self, _now=None) -> None:
        """Weekly callback: reseed the product map from primary-chain history."""
        _LOGGER.info("ProductMap: weekly rebuild starting")
        self.hass.async_create_task(self._seed_product_map())

    async def async_rebuild_map(self) -> None:
        """On-demand map rebuild (service handler)."""
        _LOGGER.info("ProductMap: on-demand rebuild requested")
        await self._seed_product_map()

    async def async_check_logins(self) -> None:
        """Probe each chain's session; only re-login the ones that are dead.

        Root rule: if we're still logged in, leave the session alone. A fresh
        Club+ login invalidates the previous working session, so an
        unconditional re-login is actively harmful. Only a clear session-expired
        signal triggers a re-login; any other probe error (e.g. a network blip)
        is left untouched. Failures are logged, not raised.
        """
        for chain, client in self._build_clients().items():
            try:
                await client.check_authed()
            except (CookieExpiredError, FoodstuffsCookieExpiredError):
                _LOGGER.info("%s session check failed — re-logging in", chain)
                try:
                    await self._require_login(chain)
                except UpdateFailed as err:
                    _LOGGER.warning("Re-login failed for %s: %s", chain, err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "%s session check errored — leaving session alone: %s",
                    chain, err,
                )
        await self.async_request_refresh()

    async def _require_login(self, chain: str) -> None:
        """Refresh login or raise UpdateFailed / trigger reauth."""
        try:
            ok = await self.async_refresh_login(chain)
        except LoginError as err:
            raise UpdateFailed(f"Login add-on unreachable: {err}") from err
        if not ok:
            self.entry.async_start_reauth(self.hass, context={"chain": chain})
            raise UpdateFailed(f"{chain} login rejected — reauth required")

    def _build_clients(self) -> dict:
        """Every configured client, degraded or not.

        Entity creation, session checks and reauth all use this — a chain
        that failed one fetch still exists and still deserves a relogin.
        """
        clients = {}
        if self.woolworths:
            clients[CHAIN_WOOLWORTHS] = self.woolworths
        clients.update(self.foodstuffs)
        return clients

    def _live_clients(self) -> dict:
        """Clients still standing this cycle. Fetch path only."""
        return {
            c: v
            for c, v in self._build_clients().items()
            if c not in self._degraded_chains
        }

    @property
    def active_chains(self) -> list[str]:
        """Return the chains that are enabled and have a usable client."""
        return list(self._build_clients())

    def is_signed_in(self, chain: str) -> bool:
        """Return True when we hold a live cookie jar for a chain.

        No jar means the next tick has to log in again; a rejected login means
        reauth is pending. The saved jar is the right state to surface.
        """
        return self._cookies.get(chain) is not None

    def set_pending_cart(self, cart: dict[str, Any] | None) -> None:
        """Stage or clear the pending cart.

        Cart staging happens in a service call, not a coordinator tick, so
        entities must be notified by hand.
        """
        self.pending_cart = cart
        self.async_update_listeners()

    async def _fetch_specials_alerts(self) -> list[dict[str, Any]]:
        """Fetch specials from all chains and intersect with the product map.

        Only items the user has actually bought before (i.e. they're in the map)
        are returned, each as:
          {name, chain, now_price, was_price, saving, barcode, product_id}
        Errors per-chain are swallowed so a single chain failure doesn't block.
        """
        alerts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()  # (barcode, chain)

        for chain, client in self._live_clients().items():
            try:
                specials = await client.get_specials()
            except Exception:
                _LOGGER.debug("Specials fetch failed for %s", chain)
                continue

            for s in specials:
                barcode = str(s.get("barcode") or "").strip()
                if not barcode:
                    continue
                if (barcode, chain) in seen:
                    continue

                entry = self.product_map.get_by_barcode(barcode)
                if not entry:
                    continue  # not a usual — skip

                seen.add((barcode, chain))
                now = s.get("now_price")
                was = s.get("was_price")
                saving = (
                    round(was - now, 2)
                    if was is not None and now is not None
                    else None
                )
                product_id = (
                    s.get("product_id")
                    or entry.get("chains", {}).get(chain)
                )
                name = s.get("name") or entry.get("name")
                if chain == CHAIN_WOOLWORTHS:
                    name = present_name(name)
                alerts.append({
                    "name": name,
                    "chain": chain,
                    "now_price": now,
                    "was_price": was,
                    "saving": saving,
                    "barcode": barcode,
                    "product_id": str(product_id) if product_id else None,
                })

        return alerts

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from grocery chain clients.

        A tick only does a fresh login when the saved cookie jar has aged out.
        Foodstuffs chains with a stale-ish token also get a cheap proactive
        refresh up front (see `_proactively_refresh_foodstuffs`), since after
        any quiet gap both banners routinely come back anonymous together.

        A mid-run session expiry on any chain triggers an immediate re-login
        and one same-cycle retry, per chain — so one chain dying (or two
        chains dying in the same cycle) doesn't blank every chain's entities
        while waiting for the next scheduled tick. Only a relogin that fails
        outright, or the SAME chain dying again right after being refreshed,
        is a real failure worth surfacing as UpdateFailed.
        """
        if not self._qty_loaded:
            self.quantities = await self._qty_store.async_load() or {}
            self._qty_loaded = True

        # Every chain gets a fresh go each tick — yesterday's failure must not
        # keep a working chain benched.
        self._degraded_chains = set()

        for chain in list(self._build_clients()):
            if self._cookies.get(chain) is None:
                try:
                    await self._require_login(chain)
                except UpdateFailed as err:
                    self._degrade(chain, str(err))

        await self._proactively_refresh_foodstuffs()

        relogged_this_cycle: set[str] = set()
        # +1 so the very last relogin still gets one retry. Each pass either
        # relogs a chain in or degrades one, so this always terminates.
        max_iterations = len(self._live_clients()) + 1
        for _ in range(max_iterations):
            if not self._live_clients():
                break
            try:
                data = await self._fetch_once()
                data["degraded_chains"] = sorted(self._degraded_chains)
                return data
            except CookieExpiredError as err:
                await self._recover_or_degrade(
                    CHAIN_WOOLWORTHS, err, relogged_this_cycle
                )
            except FoodstuffsCookieExpiredError as err:
                banner = err.banner or CHAIN_PAKNSAVE
                await self._recover_or_degrade(banner, err, relogged_this_cycle)

        # Only now is this a real failure: nothing is left to price against.
        raise UpdateFailed(
            "All chains failed this cycle — retrying next tick"
        )

    async def _proactively_refresh_foodstuffs(self) -> None:
        """Refresh Foodstuffs tokens that are close to their ~30 min idle expiry.

        Reuses `check_authed()` — the same get-current-user call the client
        already makes for a normal authed request — so a fetch after a quiet
        gap doesn't have to 401 first. Best-effort: any failure is swallowed
        and left for the normal fetch + relogin path to handle.
        """
        now = self.hass.loop.time()
        for chain, client in self.foodstuffs.items():
            if not self.is_signed_in(chain):
                continue
            last = self._foodstuffs_token_refreshed_at.get(chain, 0.0)
            if now - last < _FOODSTUFFS_TOKEN_MAX_AGE_S:
                continue
            try:
                await client.check_authed()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Proactive token refresh failed for %s: %s", chain, err)
                continue
            self._foodstuffs_token_refreshed_at[chain] = now

    async def _fetch_once(self) -> dict[str, Any]:
        """One resolve+price+specials pass. Raises the chain's cookie-expiry
        error un-caught so the caller can recover and retry."""
        resolved = await self.resolver.resolve_all(
            self.hass,
            clients=self._live_clients(),
            primary_chain=self._primary_chain(),
        )
        prices = await self._fetch_prices(resolved)
        prices = await self._substitute_out_of_stock(resolved, prices)
        await self._prune_quantities(set(resolved))
        basket_totals = self._compute_basket_totals(prices)
        specials_alerts = await self._fetch_specials_alerts()
        return {
            "resolved": resolved,
            "prices": prices,
            "basket_totals": basket_totals,
            "specials_alerts": specials_alerts,
            "quantities": dict(self.quantities),
            "primary_chain": self._primary_chain(),
        }

    async def _recover_or_degrade(
        self, chain: str, err: Exception, relogged_this_cycle: set[str]
    ) -> None:
        """Re-login `chain` right now. Returns normally (letting the caller
        retry _fetch_once immediately, same cycle) only on a successful
        relogin for a chain that hasn't already used its one relogin this
        cycle. The budget is per chain: a cycle where newworld relogs in then
        paknsave relogs in is two separate, independent successes, not a
        shared two-attempt limit.

        Every other outcome — rejected creds, an unreachable add-on, or the
        SAME chain dying again straight after its own relogin — drops just
        that chain for the rest of the cycle. It does NOT fail the update:
        one chain's login problem must never blank the chains that are
        working. `_async_update_data` raises only once nothing is left.
        """
        already_relogged = chain in relogged_this_cycle
        result = await self._retry_after_relogin(chain, err)
        if result is False:
            self._degrade(chain, "login rejected — reauth required")
            return
        if result is None:
            self._degrade(chain, "login add-on unreachable")
            return
        # result is True: relogin succeeded.
        if already_relogged:
            self._degrade(chain, "session died again right after a relogin")
            return
        relogged_this_cycle.add(chain)

    def _degrade(self, chain: str, why: str) -> None:
        """Hold `chain` out of the rest of this cycle, keeping the others."""
        _LOGGER.warning(
            "%s dropped from this update (%s) — other chains continue", chain, why
        )
        self._degraded_chains.add(chain)

    async def _retry_after_relogin(self, chain: str, err: Exception) -> bool | None:
        """Silent re-login on 401/403. Returns:
          - True  → cookies refreshed
          - False → creds rejected (reauth started)
          - None  → add-on transport failure (caller re-raises)
        """
        _LOGGER.info("Session expired for %s (%s) — re-logging in", chain, err)
        try:
            ok = await self.async_refresh_login(chain)
        except LoginError as boom:
            _LOGGER.warning("Login add-on unreachable during retry: %s", boom)
            return None
        if not ok:
            self.entry.async_start_reauth(self.hass, context={"chain": chain})
            return False
        return True

    async def _fetch_prices(self, resolved: dict[str, Any]) -> dict[str, Any]:
        """Fetch a per-chain price row for every resolved item.

        `resolved` shape is `{phrase: {chain: {product_id, confidence, reason}}}`.
        The confidence + reason ride along on each price row so the sensor
        (and, from Phase 5, the card) can flag anything needing approval.
        """
        prices: dict[str, Any] = {}

        # Woolworths — one batched pass over a shared client. get_prices
        # bounds concurrency, retries once, and warns on residual failures;
        # an unbounded per-item gather of fresh clients had Akamai dropping
        # a random handful of items every cycle. Session expiry raises
        # through so _async_update_data can re-login.
        if self.woolworths:
            ww_items = [
                (phrase, chain_ids[CHAIN_WOOLWORTHS])
                for phrase, chain_ids in resolved.items()
                if chain_ids.get(CHAIN_WOOLWORTHS)
            ]
            if ww_items:
                by_id = await self.woolworths.get_prices(
                    [res["product_id"] for _, res in ww_items]
                )
                for phrase, resolution in ww_items:
                    product_id = resolution["product_id"]
                    result = by_id.get(product_id)
                    prices.setdefault(phrase, {})[CHAIN_WOOLWORTHS] = (
                        {
                            "product_id": product_id,
                            "name": present_name(
                                result.get("name") or result.get("displayName")
                            ),
                            "price_nzd": result.get("price"),
                            "in_stock": result.get("in_stock", True),
                            "confidence": resolution.get("confidence"),
                            "reason": resolution.get("reason"),
                        }
                        if result
                        else None
                    )

        for banner, client in self.foodstuffs.items():
            banner_items = [
                (phrase, chain_ids[banner])
                for phrase, chain_ids in resolved.items()
                if chain_ids.get(banner)
            ]
            if not banner_items:
                continue
            try:
                results = await client.get_prices(
                    [res["product_id"] for _, res in banner_items]
                )
                by_id = {r["id"]: r for r in results}
            except FoodstuffsCookieExpiredError:
                # Same lesson as the Woolworths branch above: an expired
                # session must reach _async_update_data so it can re-login,
                # not be swallowed into "no prices this tick".
                raise
            except Exception:
                _LOGGER.debug("Foodstuffs price fetch failed for banner %s", banner)
                by_id = {}
            for phrase, resolution in banner_items:
                product_id = resolution["product_id"]
                result = by_id.get(product_id)
                prices.setdefault(phrase, {})[banner] = (
                    {
                        "product_id": product_id,
                        "name": result["name"],
                        "price_nzd": result["price_nzd"],
                        "in_stock": result.get("in_stock", True),
                        "confidence": resolution.get("confidence"),
                        "reason": resolution.get("reason"),
                    }
                    if result
                    else None
                )

        return prices

    @staticmethod
    def _is_unavailable(item: dict[str, Any] | None) -> bool:
        """True when a priced row is out of stock or has no real price.

        A $0 grocery price is never genuine — it's how an unavailable line
        slips through and, worse, wins a cheapest-chain comparison. Treated the
        same as an explicit out-of-stock flag.
        """
        if not item:
            return False
        return item.get("in_stock") is False or item.get("price_nzd") == 0

    async def _substitute_out_of_stock(
        self, resolved: dict[str, Any], prices: dict[str, Any]
    ) -> dict[str, Any]:
        """Re-resolve any out-of-stock pick to the next-best, once.

        The resolver commits to one product per chain with no knowledge of
        stock, so an out-of-stock first choice (e.g. the usual milk) would
        otherwise sit there at $0. Collect the unavailable picks, re-resolve
        the list once while excluding exactly those product IDs, and splice the
        substitutes back in. A single pass — a substitute that's also out of
        stock is left as-is rather than looping.
        """
        exclude: dict[str, dict[str, set[str]]] = {}
        for phrase, chain_data in prices.items():
            for chain, item in chain_data.items():
                if self._is_unavailable(item):
                    exclude.setdefault(phrase, {}).setdefault(chain, set()).add(
                        item["product_id"]
                    )
        if not exclude:
            return prices

        resolved2 = await self.resolver.resolve_all(
            self.hass,
            clients=self._live_clients(),
            primary_chain=self._primary_chain(),
            exclude=exclude,
        )
        prices2 = await self._fetch_prices(resolved2)

        for phrase, chains in exclude.items():
            for chain in chains:
                new_item = prices2.get(phrase, {}).get(chain)
                if new_item is not None:
                    resolved[phrase][chain] = resolved2[phrase][chain]
                    prices[phrase][chain] = new_item
        return prices

    def quantity_for(self, phrase: str) -> int:
        """The wanted count for a list phrase (default 1)."""
        return max(1, self.quantities.get(phrase, 1))

    async def async_set_quantity(self, phrase: str, quantity: int) -> None:
        """Set how many of a list item to buy, and re-total immediately.

        Quantity 1 is the default so it's stored as an absence. Totals are
        recomputed from the prices already in hand and pushed straight to the
        sensors — no network round-trip, the card updates within a beat.
        """
        if quantity <= 1:
            self.quantities.pop(phrase, None)
        else:
            self.quantities[phrase] = quantity
        await self._qty_store.async_save(self.quantities)
        if self.data:
            self.async_set_updated_data({
                **self.data,
                "basket_totals": self._compute_basket_totals(
                    self.data.get("prices", {})
                ),
                "quantities": dict(self.quantities),
            })

    async def _prune_quantities(self, live_phrases: set[str]) -> None:
        """Drop stored quantities for items no longer on the list."""
        stale = set(self.quantities) - live_phrases
        if not stale:
            return
        for phrase in stale:
            del self.quantities[phrase]
        await self._qty_store.async_save(self.quantities)

    def _compute_basket_totals(self, prices: dict[str, Any]) -> dict[str, float]:
        """Sum per-chain price × quantity across all items that have a price."""
        totals: dict[str, float] = {}
        for phrase, chain_data in prices.items():
            qty = self.quantity_for(phrase)
            for chain, item in chain_data.items():
                if item and item.get("price_nzd") is not None:
                    totals[chain] = totals.get(chain, 0.0) + item["price_nzd"] * qty
        return totals
