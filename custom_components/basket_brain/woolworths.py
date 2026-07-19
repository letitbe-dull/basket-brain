from __future__ import annotations

import asyncio
import logging
import re
import ssl
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

BASE_URL = "https://www.woolworths.co.nz"
_MAX_HISTORY = 250

_HEADERS = {
    "x-requested-with": "OnlineShopping.WebApp",
    "x-ui-ver": "2024.1.0",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


class CookieExpiredError(Exception):
    """Raised when the session cookies have expired (401/403)."""


def _check_response(response: httpx.Response) -> None:
    # WW-COOKIE-DIAG: does Woolworths rotate cookies on API responses? If this
    # never fires, rotation merging (the Foodstuffs fix) is irrelevant here.
    # rotated = sorted(response.cookies.keys())
    # if rotated:
    #     _LOGGER.warning(
    #         "WW-COOKIE-DIAG %s %s (%s): set_cookie=%r",
    #         response.request.method,
    #         response.request.url.path,
    #         response.status_code,
    #         rotated,
    #     )
    if response.status_code in (401, 403):
        raise CookieExpiredError(f"Session expired (HTTP {response.status_code})")
    response.raise_for_status()


_IMAGE_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)


def _barcode_from_image_url(url: str | None) -> str | None:
    """Derive the GTIN from a product image URL.

    Verified against a live response (2026-07-16): Woolworths' product
    payloads carry no `barcode` key at all — despite what this module used
    to claim. The GTIN is the image filename itself, e.g.
    `"bigImageUrl": "9339687276806.jpg"` -> `"9339687276806"`. Returns None
    if the filename isn't purely numeric (defensive — placeholder/CDN images
    don't follow this convention).
    """
    if not url:
        return None
    stem = _IMAGE_EXT_RE.sub("", url)
    return stem if stem.isdigit() else None


def _parse_price(product: dict[str, Any]) -> dict[str, Any]:
    """Flatten the nested Woolworths price object to a single `price` number.

    The API nests pricing under `price`: {"salePrice": .., "originalPrice": ..}.
    Prefer the sale price, fall back to the original.

    Also normalises `in_stock` from `availabilityStatus`. "Low Stock" is still
    purchasable — only "Out of Stock" is not.

    `barcode` (GTIN) isn't a real field on Woolworths' payloads — it's
    derived from the image filename here so callers can still treat it as
    the cross-chain key. `breadcrumb` (on detail responses) is preserved
    from the API response as-is.
    """
    price_info = product.get("price")
    if isinstance(price_info, dict):
        sale = price_info.get("salePrice")
        original = price_info.get("originalPrice")
        product["price"] = sale if sale is not None else original
    status = product.get("availabilityStatus", "")
    product["in_stock"] = status != "Out of Stock"
    if not product.get("barcode"):
        product["barcode"] = _barcode_from_image_url(
            product.get("bigImageUrl") or product.get("smallImageUrl")
        )
    return product


def present_name(name: str | None) -> str | None:
    """Title-case a Woolworths product name for display.

    Woolworths' API returns names fully lowercased ("essentials pasta penne")
    with no proper-cased field to fall back on. Capitalise the first letter of
    each word, leaving the rest untouched so intra-word bits ("up & go") survive.
    """
    if not name:
        return name
    return " ".join(w[:1].upper() + w[1:] for w in name.split(" "))


class WoolworthsClient:
    def __init__(
        self,
        cookies: dict[str, dict[str, str]] | None = None,
        store_id: str | None = None,
    ) -> None:
        self._cookies: dict[str, str] = {}
        if cookies:
            self._merge_cookies(cookies)
        self.store_id = store_id
        # Called with (shop_domain, cookies) whenever Woolworths rotates our
        # session cookies (it does so on every response — akavpau_vpwww always,
        # _abck and cw-* session cookies on some), so the owner can persist the
        # live jar. Discarding rotations is what 401'd /shoppers/my endpoints.
        self.on_cookies_rotated: Callable[[str, dict[str, str]], None] | None = None

    def update_cookies(self, cookies: dict[str, dict[str, str]]) -> None:
        """Swap in a fresh cookie jar (e.g. after a re-login).

        Accepts the `{domain: {name: value}}` shape from LoginClient. All
        `*.woolworths.co.nz` sub-jars are merged flat — no name collisions
        happen here in practice, and httpx only needs a single dict.
        """
        self._cookies.clear()
        self._merge_cookies(cookies)

    def _merge_cookies(self, cookies: dict[str, dict[str, str]]) -> None:
        for domain, jar in cookies.items():
            if "woolworths.co.nz" in domain:
                self._cookies.update(jar)

    def _client(self, extra_headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        headers = dict(_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        return httpx.AsyncClient(
            base_url=BASE_URL,
            cookies=self._cookies,
            headers=headers,
            verify=get_default_context(),
            event_hooks={"response": [self._on_response]},
        )

    async def _on_response(self, response: httpx.Response) -> None:
        """Fold rotated Set-Cookie values into our jar, like a browser.

        Error responses are skipped so a rejected request can't poison the
        jar; on success everything (Akamai + cw-* session cookies) is kept
        current and the owner is told so it can persist the live jar.
        """
        if response.status_code >= 400:
            return
        rotated = {c.name: c.value for c in response.cookies.jar if c.value}
        changed = {
            name: value
            for name, value in rotated.items()
            if self._cookies.get(name) != value
        }
        if not changed:
            return
        self._cookies.update(changed)
        _LOGGER.debug("Woolworths: merged rotated cookies %s", sorted(changed))
        if self.on_cookies_rotated:
            self.on_cookies_rotated("www.woolworths.co.nz", dict(self._cookies))

    def _mutation_headers(self) -> dict[str, str]:
        xsrf = self._cookies.get("XSRF-TOKEN", "")
        return {"x-xsrf-token": xsrf}

    async def search(self, query: str) -> list[dict[str, Any]]:
        async with self._client() as client:
            r = await client.get(
                "/api/v1/products",
                params={"target": "search", "search": query, "size": 24},
            )
            _check_response(r)
            data = r.json()
            # Shape: {"products": {"items": [...]}} — guard older/flat forms too.
            products = data.get("products") if isinstance(data, dict) else None
            if isinstance(products, dict):
                items = products.get("items", [])
            elif isinstance(products, list):
                items = products
            else:
                items = data if isinstance(data, list) else []
            return [
                _parse_price(p)
                for p in items
                if isinstance(p, dict) and p.get("type", "Product") == "Product"
            ]

    async def get_price(self, product_id: str) -> dict[str, Any]:
        async with self._client() as client:
            r = await client.get(f"/api/v1/products/{product_id}")
            _check_response(r)
            return _parse_price(r.json())

    async def get_prices(
        self, product_ids: list[str], concurrency: int = 5
    ) -> dict[str, dict[str, Any]]:
        """Fetch many product prices over one shared client.

        Firing an unbounded gather of fresh clients (one TLS handshake each)
        made Akamai drop a random handful every cycle — items then silently
        vanished from the basket. One client, a small semaphore, and a single
        retry per item keeps the burst polite and the failures rare; anything
        that still fails is logged at WARNING and left out of the result.
        Session expiry (401/403) raises so the coordinator can re-login.
        """
        sem = asyncio.Semaphore(concurrency)
        results: dict[str, dict[str, Any]] = {}

        async with self._client() as client:

            async def _one(pid: str) -> None:
                last_err: Exception | None = None
                for _attempt in range(2):
                    try:
                        async with sem:
                            r = await client.get(f"/api/v1/products/{pid}")
                        _check_response(r)
                        results[pid] = _parse_price(r.json())
                        return
                    except CookieExpiredError:
                        raise
                    except Exception as err:  # noqa: BLE001 - logged below
                        last_err = err
                _LOGGER.warning(
                    "Woolworths price fetch failed for %s after retry: %r",
                    pid, last_err,
                )

            await asyncio.gather(*(_one(pid) for pid in dict.fromkeys(product_ids)))

        return results

    async def check_authed(self) -> None:
        """Cheap authed probe — GET /bff/get-user. Raises CookieExpiredError
        on 401/403 when the session jar is dead; returns None when signed in."""
        await self.get_user()

    async def get_cart(self) -> dict[str, Any]:
        async with self._client() as client:
            r = await client.get("/api/v1/trolleys/my")
            _check_response(r)
            return r.json()

    async def add_to_cart(self, product_id: str, quantity: int) -> None:
        async with self._client(self._mutation_headers()) as client:
            # pricingUnit is "Each", not "EA".
            r = await client.post(
                "/api/v1/trolleys/my/items",
                json={
                    "sku": product_id,
                    "quantity": quantity,
                    "pricingUnit": "Each",
                },
            )
            _check_response(r)

    async def list_usual(self) -> list[dict[str, Any]]:
        """Return past-order items ranked by purchase frequency.

        Iterates genuine per-order history (not the favourites/aggregated feed).
        Dedupes by SKU across all orders; ranks by number of distinct orders the
        item appears in. Caps at _MAX_HISTORY.
        """
        async with self._client() as client:
            # --- collect order IDs (paginated order list) ---
            order_ids: list[str] = []
            page = 1
            page_size: int | None = None
            total_orders: int | None = None
            while True:
                r = await client.get(
                    "/api/v1/shoppers/my/past-orders", params={"page": page}
                )
                _check_response(r)
                data = r.json()
                orders: list[dict] = (
                    data.get("items", []) if isinstance(data, dict) else []
                )
                if not orders:
                    break
                if page_size is None:
                    page_size = len(orders)
                if total_orders is None and isinstance(data.get("totalItems"), int):
                    total_orders = data["totalItems"]
                order_ids.extend(
                    str(o["orderId"])
                    for o in orders
                    if isinstance(o, dict) and "orderId" in o
                )
                if total_orders is not None and len(order_ids) >= total_orders:
                    break  # collected everything the API advertised
                if len(orders) < page_size:
                    break  # short page — last page
                page += 1

            # --- fetch per-order line items ---
            freq: dict[str, int] = {}          # sku → order-count
            best: dict[str, dict[str, Any]] = {}  # sku → first-seen item dict
            for order_id in order_ids:
                r = await client.get(
                    f"/api/v1/shoppers/my/past-orders/{order_id}/items"
                )
                _check_response(r)
                data = r.json()
                products = data.get("products", {})
                items: list[Any] = (
                    products.get("items", []) if isinstance(products, dict) else []
                )
                seen_in_order: set[str] = set()
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sku = str(item.get("sku") or "").strip()
                    if not sku or sku in seen_in_order:
                        continue
                    seen_in_order.add(sku)
                    freq[sku] = freq.get(sku, 0) + 1
                    if sku not in best:
                        best[sku] = item

        ranked = sorted(
            best.values(),
            key=lambda i: freq.get(str(i.get("sku", "")), 0),
            reverse=True,
        )
        return [_parse_price(i) for i in ranked[:_MAX_HISTORY]]

    async def clear_cart(self) -> None:
        async with self._client(self._mutation_headers()) as client:
            r = await client.delete("/api/v1/trolleys/my/items")
            _check_response(r)

    async def get_past_orders(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            r = await client.get("/api/v1/shoppers/my/past-orders")
            _check_response(r)
            data = r.json()
            return data.get("orders", data) if isinstance(data, dict) else data

    async def get_favourites(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            r = await client.get(
                "/api/v2/shoppers/my/favourites", params={"page": 1}
            )
            _check_response(r)
            data = r.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            return [_parse_price(i) for i in (items or [])]

    async def get_breadcrumb(self, sku: str) -> dict[str, Any] | None:
        """Return the full category breadcrumb for a product (department/aisle/shelf).

        Search results only carry coarse single-level `departments`; the detail
        endpoint carries the full tree needed for category gating. Returns None on
        any error so callers can fall through gracefully.
        """
        try:
            async with self._client() as client:
                r = await client.get(f"/api/v1/products/{sku}")
                _check_response(r)
                return r.json().get("breadcrumb")
        except Exception:
            return None

    async def search_by_barcode(self, gtin: str) -> dict[str, Any] | None:
        """Resolve a GTIN barcode to a Woolworths product.

        Tries a quoted then an unquoted search, and only accepts a hit whose
        own barcode matches the requested GTIN once both are normalised (zero
        padding / GTIN-13-vs-14 differ between chains). Taking the top hit
        blind is what let a barcode lookup return an unrelated product.
        """
        target = normalise_gtin(gtin)
        if not target:
            return None
        for query in (f'"{gtin}"', gtin):
            for hit in await self.search(query):
                if normalise_gtin(hit.get("barcode")) == target:
                    return hit
        return None

    async def get_product_detail(self, product_id: str) -> dict[str, Any] | None:
        """Return full product detail (price, breadcrumb, barcode/GTIN).

        Equivalent to `get_price` but named to signal intent — the `barcode`
        (GTIN) and `breadcrumb` fields are the values the map cares about.
        Returns None on any error.
        """
        try:
            async with self._client() as client:
                r = await client.get(f"/api/v1/products/{product_id}")
                _check_response(r)
                return _parse_price(r.json())
        except Exception:
            return None

    async def get_specials(self) -> list[dict[str, Any]]:
        """Return the user's history-relevant products currently on special.

        Combines the public specials catalogue with personalised EDR offers.
        Each returned item has: barcode, product_id, name, now_price, was_price.
        Returns an empty list on any error so the caller degrades gracefully.
        """
        results: list[dict[str, Any]] = []
        try:
            async with self._client() as client:
                r = await client.get(
                    "/api/v1/products",
                    params={"target": "specials", "size": 120},
                )
                _check_response(r)
                data = r.json()
                products = data.get("products") if isinstance(data, dict) else None
                if isinstance(products, dict):
                    items = products.get("items", [])
                elif isinstance(products, list):
                    items = products
                else:
                    items = []
                for p in items:
                    if not isinstance(p, dict):
                        continue
                    price_info = p.get("price") or {}
                    if not isinstance(price_info, dict):
                        continue
                    now = price_info.get("salePrice")
                    was = price_info.get("originalPrice")
                    if now is None or was is None or now >= was:
                        continue  # not actually on special
                    results.append({
                        "barcode": str(p.get("barcode") or "").strip() or None,
                        "product_id": str(
                            p.get("sku") or p.get("stockcode") or p.get("id") or ""
                        ).strip() or None,
                        "name": p.get("name") or p.get("displayName"),
                        "now_price": round(float(now), 2),
                        "was_price": round(float(was), 2),
                    })
        except Exception:
            pass

        # EDR personalised offers (may not be available on all accounts).
        try:
            async with self._client() as client:
                r = await client.get("/api/v1/loyalty/edr/my/offers")
                if r.status_code == 200:
                    data = r.json()
                    offers = (
                        data.get("offers")
                        or data.get("items")
                        or (data if isinstance(data, list) else [])
                    )
                    for offer in offers:
                        if not isinstance(offer, dict):
                            continue
                        # EDR offer product info may be nested differently.
                        product = offer.get("product") or offer
                        barcode = str(product.get("barcode") or "").strip() or None
                        if not barcode:
                            continue
                        # Skip if already captured from the specials catalogue.
                        if any(r.get("barcode") == barcode for r in results):
                            continue
                        now = offer.get("offerPrice") or offer.get("salePrice")
                        was = offer.get("originalPrice") or offer.get("wasPrice")
                        results.append({
                            "barcode": barcode,
                            "product_id": str(
                                product.get("sku") or product.get("stockcode") or ""
                            ).strip() or None,
                            "name": product.get("name") or product.get("displayName"),
                            "now_price": round(float(now), 2) if now else None,
                            "was_price": round(float(was), 2) if was else None,
                        })
        except Exception:
            pass

        return results

    async def get_timeslots(self) -> dict[str, Any]:
        async with self._client() as client:
            r = await client.get("/api/v1/fulfilment/time-slots-summary")
            _check_response(r)
            return r.json()

    async def get_user(self) -> dict[str, Any]:
        async with self._client() as client:
            r = await client.get("/api/v1/bff/get-user")
            _check_response(r)
            return r.json()

    async def set_store(self) -> None:
        """Bind the account to the configured pickup store.

        PUT /api/v1/fulfilment/my/pickup-addresses with the store's integer
        addressId. x-xsrf-token is not required here but sent for consistency.
        """
        if not self.store_id:
            return
        async with self._client(self._mutation_headers()) as client:
            r = await client.put(
                "/api/v1/fulfilment/my/pickup-addresses",
                json={"addressId": int(self.store_id)},
            )
            _check_response(r)


async def fetch_stores() -> list[dict[str, Any]]:
    """Return the Woolworths pickup store list for the config-flow picker.

    Uses an anonymous session (no login needed). Returns [{id, name, address}].
    """
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=_HEADERS,
        timeout=15,
        verify=get_default_context(),
    ) as client:
        await client.post("/api/v1/session", json={})
        r = await client.get("/api/v1/addresses/pickup-addresses")
        r.raise_for_status()
        data = r.json()

    seen: set[str] = set()
    stores: list[dict[str, Any]] = []
    for area in data.get("storeAreas", []):
        for s in area.get("storeAddresses", []):
            store_id = str(s.get("id"))
            if not store_id or store_id in seen:
                continue
            seen.add(store_id)
            stores.append(
                {
                    "id": store_id,
                    "name": (s.get("name") or "").strip(),
                    "address": s.get("address"),
                }
            )
    return stores
