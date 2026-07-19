from __future__ import annotations

import base64
import binascii
import json
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any

import httpx

from .product_utils import normalise_gtin

try:
    # HA's shared, pre-warmed SSL context — avoids blocking cert loads in
    # the event loop every time we build a client.
    from homeassistant.util.ssl import get_default_context
except ImportError:  # standalone use (e.g. scripts/fetch_store_lists.py)
    get_default_context = ssl.create_default_context

_LOGGER = logging.getLogger(__name__)

_TOKEN_TTL = 3500  # seconds; actual token lives ~1 hour
# How many search candidates to detail-check when resolving a barcode.
_BARCODE_VERIFY_LIMIT = 5

# The Foodstuffs endpoints 403 requests without browser-like headers.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) "
        "Gecko/20100101 Firefox/130.0"
    ),
    "Accept": "application/json",
}


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (diagnostics only).

    `roles: ["SHOPPER"]` when authenticated, `["ANONYMOUS"]` when not.
    The JSON wrapper never carries `roles`, so the token is the only signal.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore stripped padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return {}


class FoodstuffsAuthError(Exception):
    """Raised when an anonymous token cannot be obtained."""


class FoodstuffsCookieExpiredError(FoodstuffsAuthError):
    """Raised when a user-scoped call fails auth — session cookies missing/expired."""

    def __init__(self, message: str, banner: str | None = None) -> None:
        super().__init__(message)
        self.banner = banner


class FoodstuffsClient:
    """Client for the Foodstuffs REST API (PAK'nSAVE and New World)."""

    def __init__(
        self,
        banner: str,
        store_id: str,
        cookies: dict[str, dict[str, str]] | None = None,
    ) -> None:
        if banner not in ("paknsave", "newworld"):
            raise ValueError(f"Unknown Foodstuffs banner: {banner!r}")
        if not store_id:
            raise ValueError("store_id must not be empty")

        self.banner = banner
        self.store_id = store_id
        self._shop_domain = f"www.{banner}.co.nz"
        # Shop-domain cookies only. Club+ (`login.clubplus.co.nz`) cookies are
        # useless to the shop API and — worse — carry a `refresh_token` that
        # collides with the real shop-domain one; mixing them silently makes
        # the token endpoint mint an anonymous token.
        self._cookies: dict[str, str] = {}
        if cookies:
            self._cookies.update(cookies.get(self._shop_domain, {}))
        self._api_host = f"https://api-prod.{banner}.co.nz"
        self._token: str | None = None
        self._token_expiry: float = 0.0
        # Called with (shop_domain, cookies) whenever the server rotates our
        # session cookies, so the owner can persist the live jar. A browser
        # honours every Set-Cookie; discarding them is what killed sessions
        # at the ~30 min mark.
        self.on_cookies_rotated: Callable[[str, dict[str, str]], None] | None = None

    def update_cookies(self, cookies: dict[str, dict[str, str]]) -> None:
        """Swap in a fresh cookie jar and invalidate the cached bearer token.

        Only shop-domain cookies are kept — Club+ cookies carry a
        `refresh_token` that collides with the shop-domain one and breaks auth.
        """
        self._cookies.clear()
        self._cookies.update(cookies.get(self._shop_domain, {}))
        self._token = None
        self._token_expiry = 0.0

    async def _get_user_token(self) -> str:
        """Return a cached user-scoped bearer token, refreshing if expired.

        With valid session cookies the endpoint mints a SHOPPER token.
        Without them it mints an anonymous one, which 400s on cart writes
        with "Store is not defined" — raise up front instead.
        """
        if self._token and time.monotonic() < self._token_expiry:
            return self._token

        url = f"https://{self._shop_domain}/api/user/get-current-user"
        async with httpx.AsyncClient(
            timeout=15,
            cookies=self._cookies,
            headers={
                **_BROWSER_HEADERS,
                "Origin": f"https://{self._shop_domain}",
                "Referer": f"https://{self._shop_domain}/",
            },
            verify=get_default_context(),
        ) as client:
            try:
                # POST with empty body — GET returns 405.
                resp = await client.post(url, json={})
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as err:
                raise FoodstuffsAuthError(
                    f"Token fetch failed ({err.response.status_code})"
                ) from err
            except httpx.RequestError as err:
                raise FoodstuffsAuthError(f"Token fetch network error: {err}") from err

        transition = data.get("transition")
        token: str | None = (
            data.get("access_token")
            or data.get("token")
            or data.get("accessToken")
        )
        if not token:
            raise FoodstuffsAuthError(
                f"Bearer token not found in response: {list(data.keys())}"
            )

        claims = _jwt_claims(token)
        roles = claims.get("roles")
        # _LOGGER.warning(
        #     "TOKEN-DIAG %s: transition=%r roles=%r sub=%r email=%r "
        #     "set_cookie=%r cookies_sent=%r",
        #     self.banner,
        #     transition,
        #     roles,
        #     claims.get("sub"),
        #     claims.get("email"),
        #     # cookies the server rotated this response (merged below on success)
        #     sorted(resp.cookies.keys()),
        #     sorted(self._cookies),
        # )
        if isinstance(roles, list) and "ANONYMOUS" in roles:
            raise FoodstuffsCookieExpiredError(
                f"Foodstuffs {self.banner} token is anonymous "
                f"(transition={transition!r}) — session cookies "
                "missing or expired",
                banner=self.banner,
            )

        self._merge_rotated_cookies(resp)
        self._token = token
        self._token_expiry = time.monotonic() + _TOKEN_TTL
        return token

    def _merge_rotated_cookies(self, resp: httpx.Response) -> None:
        """Fold the response's Set-Cookie values into our jar, like a browser.

        Only called on a live SHOPPER response — an anonymous fs-user-token
        from a dead session must never overwrite (or be persisted into) the
        jar. All cookies here are shop-domain: the request went to
        www.<banner>, so no Club+ collision is possible.
        """
        rotated = {c.name: c.value for c in resp.cookies.jar if c.value}
        changed = {
            name: value
            for name, value in rotated.items()
            if self._cookies.get(name) != value
        }
        if not changed:
            return
        self._cookies.update(changed)
        _LOGGER.debug(
            "Foodstuffs %s: merged rotated cookies %s",
            self.banner, sorted(changed),
        )
        if self.on_cookies_rotated:
            self.on_cookies_rotated(self._shop_domain, dict(self._cookies))

    async def check_authed(self) -> None:
        """Cheap authed probe — mint a user token from the session cookies.

        Clears any cached token first so this is a real network check, then
        raises FoodstuffsCookieExpiredError when the token comes back anonymous
        (session gone). Returns None when still a live SHOPPER session.
        """
        self._token = None
        self._token_expiry = 0.0
        await self._get_user_token()

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._get_user_token()
        return {"Authorization": f"Bearer {token}"}

    async def _authed_get(self, path: str) -> Any:
        # api-prod takes a bearer token only — no cookies.
        headers = await self._auth_headers()
        async with httpx.AsyncClient(
            timeout=15,
            headers=_BROWSER_HEADERS,
            verify=get_default_context(),
        ) as client:
            resp = await client.get(f"{self._api_host}{path}", headers=headers)
            if resp.status_code in (401, 403):
                # Token may have expired mid-session — force refresh once.
                self._token = None
                headers = await self._auth_headers()
                resp = await client.get(f"{self._api_host}{path}", headers=headers)
            if resp.status_code in (401, 403):
                raise FoodstuffsCookieExpiredError(
                    f"Foodstuffs auth failed (HTTP {resp.status_code})",
                    banner=self.banner,
                )
            if resp.status_code >= 400:
                _LOGGER.error(
                    "Foodstuffs %s GET %s failed (%s): %s",
                    self.banner, path, resp.status_code, resp.text[:500],
                )
            resp.raise_for_status()
            return resp.json()

    async def _authed_post(self, path: str, body: dict[str, Any]) -> Any:
        headers = await self._auth_headers()
        async with httpx.AsyncClient(
            timeout=15,
            headers=_BROWSER_HEADERS,
            verify=get_default_context(),
        ) as client:
            resp = await client.post(
                f"{self._api_host}{path}", json=body, headers=headers
            )
            if resp.status_code in (401, 403):
                self._token = None
                headers = await self._auth_headers()
                resp = await client.post(
                    f"{self._api_host}{path}", json=body, headers=headers
                )
            if resp.status_code in (401, 403):
                raise FoodstuffsCookieExpiredError(
                    f"Foodstuffs auth failed (HTTP {resp.status_code})",
                    banner=self.banner,
                )
            if resp.status_code >= 400:
                _LOGGER.error(
                    "Foodstuffs %s POST %s failed (%s): body=%s response=%s",
                    self.banner, path, resp.status_code, body, resp.text[:500],
                )
            resp.raise_for_status()
            # Some endpoints (e.g. cart/store/{id}) answer 200 with no body.
            return resp.json() if resp.content else None

    async def search(self, query: str) -> list[dict[str, Any]]:
        body = {
            "algoliaQuery": {"query": query},
            "storeId": self.store_id,
            "hitsPerPage": 24,
            "page": 0,
            "sortOrder": "NI_POPULARITY_ASC",
        }
        data = await self._authed_post("/v1/edge/search/paginated/products", body)
        products: list[dict[str, Any]] = data.get("products", []) or []
        return [_normalise_product(p) for p in products]

    async def get_price(self, product_id: str) -> dict[str, Any]:
        results = await self.get_prices([product_id])
        if not results:
            return {
                "id": product_id,
                "name": None,
                "price_nzd": None,
                "in_stock": False,
            }
        return results[0]

    async def get_cart(self) -> dict[str, Any]:
        return await self._authed_get("/v1/edge/cart")

    async def add_to_cart(self, product_id: str, quantity: int) -> None:
        """Set a product's cart quantity (absolute, not incremental).

        `sale_type` is snake_case and NO `storeId` in the body — store context
        rides on the user-scoped token. Requires valid session cookies.
        """
        body = {
            "products": [
                {"productId": product_id, "quantity": quantity, "sale_type": "UNITS"}
            ]
        }
        await self._authed_post("/v1/edge/cart", body)

    async def list_usual(self) -> list[dict[str, Any]]:
        """Return previously-bought items, most-bought first.

        Primary: GET /v1/edge/order/purchasedproducts/top/50 (frequency-ranked,
        ready-made). Items carry `gtin` directly — no separate detail call needed
        for barcode-match. Falls back to the system `abandoned_list` (previous
        trolley, unranked) when the account has no order/Clubcard history.
        """
        try:
            data = await self._authed_get("/v1/edge/order/purchasedproducts/top/250")
            products: list[dict[str, Any]] = (
                data.get("products", []) if isinstance(data, dict) else []
            )
            if products:
                return [
                    {
                        "name": p.get("name"),
                        "productId": p.get("productId"),
                        "brand": p.get("brand"),
                        "categoryTrees": p.get("categoryTrees") or [],
                        "barcode": p.get("gtin") or p.get("sku"),
                    }
                    for p in products
                    if p.get("productId")
                ]
        except Exception:
            _LOGGER.debug(
                "Foodstuffs %s purchasedproducts/top unavailable — "
                "trying abandoned_list",
                self.banner,
            )

        # Fallback: abandoned_list (previous trolley, flat, unranked)
        lists_data = await self._authed_get("/v1/edge/list")
        lists = lists_data.get("lists", []) if isinstance(lists_data, dict) else []
        if not any(entry.get("listId") == "abandoned_list" for entry in lists):
            return []

        detail = await self._authed_get("/v1/edge/list/abandoned_list")
        products = detail.get("products", []) if isinstance(detail, dict) else []
        return [
            {
                "name": p.get("name"),
                "productId": p.get("productId"),
                "brand": p.get("brand"),
                "categoryTrees": p.get("categoryTrees") or [],
                "barcode": None,
            }
            for p in products
            if p.get("productId")
        ]

    async def get_prices(self, product_ids: list[str]) -> list[dict[str, Any]]:
        """Bulk price lookup via decorateProducts."""
        if not product_ids:
            return []
        body = {"productIds": product_ids}
        data = await self._authed_post(
            f"/v1/edge/store/{self.store_id}/decorateProducts", body
        )
        products: list[dict[str, Any]] = data if isinstance(data, list) else (
            data.get("products") or data.get("items") or []
        )
        return [_normalise_decorated(p) for p in products]

    async def get_stores(self) -> list[dict[str, Any]]:
        """Return store list for config-flow store picker (anonymous)."""
        return await fetch_stores(self.banner)

    async def set_store(self) -> None:
        """Bind the cart to the configured store. Must run before any cart write.

        POST /v1/edge/cart/store/{storeId} on api-prod, bearer-authed.
        Without it, cart writes 400 with "Store is not defined".

        Not `/api/store-regionalisation` — that's regional page content only
        and returns 200 regardless, setting nothing on the account.
        """
        await self._authed_post(f"/v1/edge/cart/store/{self.store_id}", {})

    async def get_next_slot(self) -> str | None:
        """Return the earliest available click & collect slot, or None.

        `available` is remaining capacity; 0 means fully booked.
        Returns the first `available > 0` entry in API order (soonest first).
        """
        data = await self._authed_get(
            f"/v1/edge/store/{self.store_id}/clickAndCollectSlots?type=COMBINED"
        )
        days = data.get("slots", []) if isinstance(data, dict) else []
        for day in days:
            date = day.get("date")
            for slot in day.get("timeSlots", []) or []:
                if (slot.get("available") or 0) > 0:
                    return f"{date} {slot.get('slot')}"
        return None


    async def get_product_detail(self, canonical_id: str) -> dict[str, Any] | None:
        """Return full product detail including the GTIN barcode.

        The `sku` field on this endpoint IS the GTIN — the universal cross-chain
        key. The `productId` (e.g. `5017010-EA-000`) is distinct from it.
        PAK'nSAVE and New World share the same `productId` — one lookup covers both.
        Returns None on any error.
        """
        try:
            data = await self._authed_get(
                f"/v1/edge/store/{self.store_id}/product/{canonical_id}"
            )
            single = data.get("singlePrice") or {}
            price_cents = single.get("price") or data.get("price")
            return {
                "id": data.get("productId") or data.get("id"),
                "name": data.get("name") or data.get("displayName"),
                "brand": data.get("brand"),
                "barcode": data.get("sku"),  # The GTIN
                "categoryTrees": data.get("categoryTrees") or [],
                "price_nzd": _cents_to_dollars(price_cents),
            }
        except Exception:
            return None

    async def search_by_barcode(self, gtin: str) -> dict[str, Any] | None:
        """Resolve a GTIN barcode to a Foodstuffs product.

        Search hits do NOT carry the GTIN (see `_normalise_product`), so we
        can't confirm a match on the hit alone — comparing the hit's `sku`
        against the GTIN silently failed for almost every product. Instead we
        send the GTIN as an Algolia query and, for the top few candidates,
        fetch product detail (whose `sku` IS the GTIN) and compare on a
        normalised barcode. Algolia usually returns the exact product first, so
        this is normally a single detail call. Returns the normalised product
        dict, or None when no candidate's barcode matches.
        """
        target = normalise_gtin(gtin)
        if not target:
            return None
        body = {
            "algoliaQuery": {"query": gtin},
            "storeId": self.store_id,
            "hitsPerPage": 24,
            "page": 0,
            "sortOrder": "NI_POPULARITY_ASC",
        }
        data = await self._authed_post("/v1/edge/search/paginated/products", body)
        for raw in (data.get("products", []) or [])[:_BARCODE_VERIFY_LIMIT]:
            pid = raw.get("productId") or raw.get("id") or raw.get("objectID")
            if not pid:
                continue
            detail = await self.get_product_detail(str(pid))
            if detail and normalise_gtin(detail.get("barcode")) == target:
                return _normalise_product(raw)
        return None

    async def get_specials(self) -> list[dict[str, Any]]:
        """Return the user's history-relevant products currently on special.

        Combines personalised promotions with relevant offers from both
        Foodstuffs endpoints. Each returned item has: barcode, product_id,
        name, now_price, was_price. Empty list on any error.
        """
        results: list[dict[str, Any]] = []
        seen_barcodes: set[str] = set()

        for path in (
            "/product/personalisedPromotions",
            "/product/relevantOffers",
        ):
            try:
                data = await self._authed_get(path)
            except Exception:
                continue

            items = (
                data.get("products")
                or data.get("promotions")
                or data.get("offers")
                or data.get("items")
                or (data if isinstance(data, list) else [])
            )
            for p in items or []:
                if not isinstance(p, dict):
                    continue
                # Each entry may be a promotion wrapper or a product directly.
                product = p.get("product") or p
                product_id = str(
                    product.get("productId") or product.get("id") or ""
                ).strip() or None

                # Barcode (GTIN) is on the product detail endpoint; promotions
                # may carry it as `gtin` or `sku` directly.
                barcode = str(
                    product.get("gtin") or product.get("sku") or ""
                ).strip() or None

                if not barcode or barcode in seen_barcodes:
                    continue
                seen_barcodes.add(barcode)

                single = product.get("singlePrice") or {}
                now_cents = (
                    p.get("price")
                    or p.get("promotionPrice")
                    or single.get("price")
                )
                was_cents = (
                    p.get("originalPrice")
                    or p.get("wasPrice")
                    or single.get("originalPrice")
                    or single.get("wasPrice")
                )
                results.append({
                    "barcode": barcode,
                    "product_id": product_id,
                    "name": product.get("name") or product.get("displayName"),
                    "now_price": _cents_to_dollars(now_cents),
                    "was_price": _cents_to_dollars(was_cents),
                })

        return results

    async def get_breadcrumb(self, product_id: str) -> dict[str, Any] | None:
        """Not applicable — Foodstuffs ships `categoryTrees` inline on search results.

        Exists to satisfy the GroceryClient protocol. Always returns None;
        callers should read `categoryTrees` from the normalised search/list dict.
        """
        return None


async def fetch_stores(banner: str) -> list[dict[str, Any]]:
    """Return the store list for a banner (anonymous — used by the store picker)."""
    if banner not in ("paknsave", "newworld"):
        raise ValueError(f"Unknown Foodstuffs banner: {banner!r}")
    host = f"https://api-prod.{banner}.co.nz"
    async with httpx.AsyncClient(
        timeout=15, headers=_BROWSER_HEADERS, verify=get_default_context()
    ) as client:
        tr = await client.post(
            f"https://www.{banner}.co.nz/api/user/get-current-user", json={}
        )
        tr.raise_for_status()
        token = tr.json().get("access_token")
        resp = await client.get(
            f"{host}/v1/edge/store", headers={"Authorization": f"Bearer {token}"}
        )
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("stores") or data.get("items") or []


def _cents_to_dollars(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(value / 100, 2)


def _normalise_product(hit: dict[str, Any]) -> dict[str, Any]:
    """Normalise a search hit. Price is in cents under `singlePrice.price`.

    `categoryTrees` (3-level inline) and `boughtBefore` are preserved here
    because they are used by the resolver for category gating and history
    seeding. `barcode` is absent from search results — use `get_product_detail`
    to retrieve the GTIN for a specific product.
    """
    single = hit.get("singlePrice") or {}
    price_cents = (
        single.get("price")
        or hit.get("price")
        or hit.get("salePrice")
    )
    return {
        "id": hit.get("productId") or hit.get("id") or hit.get("objectID"),
        "name": hit.get("name") or hit.get("productName"),
        "price_nzd": _cents_to_dollars(price_cents),
        "unit": hit.get("displayName") or hit.get("unit"),
        "barcode": None,  # Not on search hits; populated by get_product_detail
        "categoryTrees": hit.get("categoryTrees") or [],
        "boughtBefore": hit.get("boughtBefore", False),
    }


def _normalise_decorated(product: dict[str, Any]) -> dict[str, Any]:
    """Normalise a decorateProducts entry. Price is cents under `singlePrice.price`;
    stock is an `availability` list e.g. `["IN_STORE", "ONLINE"]`."""
    single = product.get("singlePrice") or {}
    price_cents = single.get("price") or product.get("price")
    availability = product.get("availability") or []
    in_stock = (
        "ONLINE" in availability
        if isinstance(availability, list)
        else bool(product.get("inStock"))
    )
    return {
        "id": product.get("productId") or product.get("id"),
        "name": product.get("name") or product.get("productName"),
        "price_nzd": _cents_to_dollars(price_cents),
        "in_stock": in_stock,
    }
