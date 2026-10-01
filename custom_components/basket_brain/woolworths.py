"""Woolworths NZ client over the site's GraphQL endpoint.

Shapes, units and traps: docs/WOOLWORTHS-API-CHANGE.md and docs/woolworths-graphql-probes.md.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Callable
from typing import Any

import httpx

try:
    # HA's shared, pre-warmed SSL context — avoids blocking cert loads in
    # the event loop every time we build a client.
    from homeassistant.util.ssl import get_default_context
except ImportError:  # standalone use (e.g. scripts/fetch_store_lists.py)
    get_default_context = ssl.create_default_context

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.woolworths.co.nz"
GRAPHQL_PATH = "/api/graphql"
_MAX_HISTORY = 250
_TIMEOUT = 20

# Akamai drops connections whose user-agent says python-httpx; this one is accepted.
_HEADERS = {
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

_OUT_OF_STOCK = frozenset({"OUT_OF_STOCK", "OutOfStock", "UNAVAILABLE", "Unavailable"})
_PICKUP_MODE = "Pickup"
_ORDERS_PAGE_SIZE = 20
_SPECIALS_PAGE_SIZE = 500
_SPECIALS_MAX_PAGES = 10

_ME = "query Me { me { __typename id } }"

_PRICE_FIELDS = "variantPrice { sellingPrice wasPrice isSpecial }"

_DETAIL = f"""query GetProductDetails($key: String!) {{
  My {{ product(key: $key) {{
    key brand name storeId
    category {{ key name level
      parent {{ key name level parent {{ key name level }} }} }}
    variants {{ ... on GroceryVariant {{
      key sku barcode volumeSize availabilityStatus averageWeight
      purchasingUnits {{ unit incrementQty minimumQty }}
      {_PRICE_FIELDS} }} }}
  }} }}
}}"""

_SEARCH = f"""query ProductSearch($searchInput: CompositeSearchInput!) {{
  My {{ products(searchInput: $searchInput) {{ results {{
    __typename
    ... on ProductSummary {{ sku productName brand
      variants {{ variantKey unitOfMeasure availabilityStatus
        purchaseUnit {{ unit incrementQty minimumQty }}
        {_PRICE_FIELDS} }} }}
  }} }} }}
}}"""

_CART = """query CustomerCart { customerCart {
  key
  lineItems { sku productVariantSku quantity }
} }"""

_PUSH = """mutation SetCartLineItemQuantity($input: SetCartLineItemQuantitiesInput!) {
  setCartLineItemQuantity(input: $input) { key totalItemQuantity }
}"""

_ORDERS = """query Orders($input: OrdersInput!) {
  orders(input: $input) { totalPages results { orderNumber } }
}"""

_ORDER_DETAILS = """query OrderDetails($orderNumber: ID!) {
  order(orderNumber: $orderNumber) { lineItems { productKey product { name } } }
}"""

_PROPOSITIONS = """query Propositions($input: PropositionsInput!) {
  propositions(input: $input) { propositions { id name method available startTime } }
}"""

_SET_SHOPPING_MODE = """mutation SetCartShoppingMode($setCartShoppingModeInput: SetCartShoppingModeInput!) {
  setCartShoppingMode(input: $setCartShoppingModeInput) {
    shoppingMode { mode pickupLocationId }
  }
}"""

_LOCATIONS = """query SearchLocations($input: LocationsInput!) {
  locations(input: $input) { locations {
    id name storeId
    address { locality { suburb city } lines { line1 } }
  } }
}"""


class CookieExpiredError(Exception):
    """Raised when the session is not signed in."""


class GraphQLError(Exception):
    """Raised when a GraphQL call returns errors, no data, or a non-200 status."""


async def _gql(
    client: httpx.AsyncClient, op: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """POST one GraphQL operation and return its `data`.

    @param client: open client on BASE_URL
    @param op: operation name
    @param query: operation text
    @param variables: operation variables
    @returns the response's `data` object
    """
    r = await client.post(
        f"{GRAPHQL_PATH}?op-name={op}",
        json={"operationName": op, "query": query, "variables": variables or {}},
    )
    if r.status_code in (401, 403):
        raise CookieExpiredError(f"Session expired (HTTP {r.status_code})")
    try:
        body = r.json()
    except ValueError:
        body = None
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        messages = "; ".join(str(e.get("message")) for e in errors if isinstance(e, dict))
        codes = {(e.get("extensions") or {}).get("code") for e in errors if isinstance(e, dict)}
        level = logging.DEBUG if "BANNED_OPERATION" in codes else logging.WARNING
        _LOGGER.log(level, "Woolworths %s failed: %s", op, messages)
        raise GraphQLError(f"{op}: {messages}")
    if r.status_code != 200:
        _LOGGER.warning("Woolworths %s failed: HTTP %s", op, r.status_code)
        raise GraphQLError(f"{op}: HTTP {r.status_code}")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        _LOGGER.warning("Woolworths %s failed: no data", op)
        raise GraphQLError(f"{op}: no data")
    return data


def _variant_key(variant: dict[str, Any]) -> str:
    """Variant key, from detail (`key`) or search (`variantKey`) shapes.

    @param variant: one variant
    @returns key such as `282848-EA`, or ""
    """
    return str(variant.get("key") or variant.get("variantKey") or "")


def _variant_unit(variant: dict[str, Any]) -> str | None:
    """Sale unit of a variant: key suffix, then purchasing unit, then unit of measure.

    @param variant: one variant
    @returns "EA", "KG" or None
    """
    suffix = _variant_key(variant).rpartition("-")[2].upper()
    if suffix in ("EA", "KG"):
        return suffix
    units = variant.get("purchasingUnits") or variant.get("purchaseUnit")
    if isinstance(units, list):
        units = units[0] if units else None
    unit = str((units or {}).get("unit") or variant.get("unitOfMeasure") or "").upper()
    return {"EACH": "EA", "EA": "EA", "KG": "KG"}.get(unit)


def _variants(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Variant dicts of a product or search row.

    @param raw: product or ProductSummary
    @returns variants that are dicts
    """
    return [v for v in raw.get("variants") or [] if isinstance(v, dict)]


def _priced_variant(variants: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The variant a price row uses: EA when sold each, else KG.

    @param variants: product variants
    @returns chosen variant or None
    """
    each = [v for v in variants if _variant_unit(v) == "EA"]
    weight = [v for v in variants if _variant_unit(v) == "KG"]
    chosen = each or weight or variants
    return chosen[0] if chosen else None


def _money(value: Any) -> float | None:
    """A positive dollar amount, or None.

    @param value: raw price
    @returns rounded dollars, or None when absent, unparseable or not positive
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2) if value > 0 else None


def _real_gtin(value: Any) -> str | None:
    """Return the value if it is a real GTIN, not a scale or store-internal code.

    @param value: raw barcode
    @returns the digits, or None for scale codes, restricted-circulation and malformed values
    """
    digits = str(value or "").strip()
    if not digits.isdigit():
        return None
    if len(digits) == 8:
        return None if digits[0] in "02" else digits
    if len(digits) not in (12, 13, 14):
        return None
    prefix = digits.zfill(14)[1:3]
    if prefix == "02" or "20" <= prefix <= "29":
        return None
    return digits


def _breadcrumb(categories: Any) -> dict[str, dict[str, str]] | None:
    """Department/aisle/shelf from the one level-3 category entry.

    @param categories: the product's flat `category` list
    @returns {department, aisle, shelf} each {name}, or None
    """
    for c in categories or []:
        if not isinstance(c, dict) or c.get("level") != 3:
            continue
        aisle = c.get("parent") or {}
        dept = aisle.get("parent") or {}
        if aisle.get("level") == 2 and dept.get("level") == 1:
            return {
                "department": {"name": str(dept.get("name") or "")},
                "aisle": {"name": str(aisle.get("name") or "")},
                "shelf": {"name": str(c.get("name") or "")},
            }
    return None


def _parse_product(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten a detail product or search row to the client's product shape.

    @param raw: GraphQL product or ProductSummary
    @returns {sku, name, brand, price, was_price, in_stock, kg_only, store_id}
    """
    variants = _variants(raw)
    chosen = _priced_variant(variants) or {}
    price_info = chosen.get("variantPrice") or {}
    status = str(chosen.get("availabilityStatus") or "")
    return {
        "sku": str(raw.get("key") or raw.get("sku") or ""),
        "name": raw.get("name") or raw.get("productName"),
        "brand": raw.get("brand"),
        "price": _money(price_info.get("sellingPrice")),
        "was_price": _money(price_info.get("wasPrice")),
        "in_stock": status not in _OUT_OF_STOCK,
        "kg_only": bool(variants) and all(_variant_unit(v) == "KG" for v in variants),
        "store_id": raw.get("storeId") or raw.get("storeKey"),
    }


def _parse_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """Product shape plus barcode, breadcrumb and size from a detail product.

    @param raw: GraphQL detail product
    @returns parsed product with `barcode`, `breadcrumb`, `size`
    """
    product = _parse_product(raw)
    variants = _variants(raw)
    first_barcode = next((v.get("barcode") for v in variants if v.get("barcode")), None)
    volume = next((v.get("volumeSize") for v in variants if v.get("volumeSize")), None)
    product["barcode"] = _real_gtin(first_barcode)
    product["breadcrumb"] = _breadcrumb(raw.get("category"))
    product["size"] = {"volumeSize": volume} if volume else None
    return product


def _search_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """ProductSummary rows of a ProductSearch result, ads and sponsored rows dropped.

    @param data: ProductSearch data
    @returns rows that carry a sku
    """
    results = ((data.get("My") or {}).get("products") or {}).get("results") or []
    return [r for r in results if isinstance(r, dict) and r.get("sku")]


def present_name(name: str | None) -> str | None:
    """Title-case a Woolworths product name for display.

    @param name: product name
    @returns name with each word's first letter upper-cased
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
        # Called with (shop_domain, cookies) whenever Woolworths rotates session
        # cookies, so the owner can persist the live jar.
        self.on_cookies_rotated: Callable[[str, dict[str, str]], None] | None = None

    def update_cookies(self, cookies: dict[str, dict[str, str]]) -> None:
        """Swap in a fresh `{domain: {name: value}}` cookie jar.

        @param cookies: jar from LoginClient
        """
        self._cookies.clear()
        self._merge_cookies(cookies)

    def _merge_cookies(self, cookies: dict[str, dict[str, str]]) -> None:
        for domain, jar in cookies.items():
            if "woolworths.co.nz" in domain:
                self._cookies.update(jar)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL,
            cookies=self._cookies,
            headers=_HEADERS,
            timeout=_TIMEOUT,
            verify=get_default_context(),
            event_hooks={"response": [self._on_response]},
        )

    async def _on_response(self, response: httpx.Response) -> None:
        """Fold rotated Set-Cookie values into the jar and tell the owner.

        @param response: any successful response
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

    async def _ensure_signed_in(self, client: httpx.AsyncClient) -> None:
        """Raise CookieExpiredError unless `Me` answers with a Customer.

        @param client: open client
        """
        try:
            data = await _gql(client, "Me", _ME)
        except (GraphQLError, httpx.HTTPError) as err:
            raise CookieExpiredError(f"Woolworths session not signed in: {err}") from err
        if (data.get("me") or {}).get("__typename") != "Customer":
            raise CookieExpiredError("Woolworths session not signed in")

    async def _detail(self, client: httpx.AsyncClient, product_id: str) -> dict[str, Any] | None:
        """Fetch and parse one product's detail.

        @param client: open client
        @param product_id: bare SKU
        @returns parsed detail, or None when Woolworths has no such product
        """
        data = await _gql(client, "GetProductDetails", _DETAIL, {"key": str(product_id)})
        raw = (data.get("My") or {}).get("product")
        return _parse_detail(raw) if isinstance(raw, dict) else None

    async def check_authed(self) -> None:
        """Raise CookieExpiredError unless the jar is a signed-in session."""
        async with self._client() as client:
            await self._ensure_signed_in(client)

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Keyword search, first 24 product rows.

        @param query: search phrase
        @returns parsed product rows
        """
        async with self._client() as client:
            data = await _gql(client, "ProductSearch", _SEARCH, {
                "searchInput": {"byKeyword": {
                    "value": query, "sortBy": "RELEVANCE", "pageIndex": 0, "pageSize": 24,
                }},
            })
        return [_parse_product(r) for r in _search_rows(data)]

    async def get_price(self, product_id: str) -> dict[str, Any]:
        """Price one product.

        @param product_id: bare SKU
        @returns parsed product with a price
        """
        result = (await self.get_prices([product_id])).get(product_id)
        if result is None:
            raise GraphQLError(f"No Woolworths price for {product_id}")
        return result

    async def get_prices(
        self, product_ids: list[str], concurrency: int = 5
    ) -> dict[str, dict[str, Any]]:
        """Price many products over one client, after confirming the session.

        @param product_ids: bare SKUs
        @param concurrency: requests in flight
        @returns {sku: parsed product}; SKUs without a readable price are left out
        """
        sem = asyncio.Semaphore(concurrency)
        results: dict[str, dict[str, Any]] = {}

        async with self._client() as client:
            await self._ensure_signed_in(client)

            async def _one(pid: str) -> None:
                last_err: Exception | None = None
                for _attempt in range(2):
                    try:
                        async with sem:
                            detail = await self._detail(client, pid)
                    except CookieExpiredError:
                        raise
                    except Exception as err:  # noqa: BLE001 - logged below
                        last_err = err
                        continue
                    if detail and detail["price"] is not None:
                        results[pid] = detail
                    return
                _LOGGER.warning(
                    "Woolworths price fetch failed for %s after retry: %r", pid, last_err
                )

            await asyncio.gather(*(_one(pid) for pid in dict.fromkeys(product_ids)))

        return results

    async def get_product_detail(self, product_id: str) -> dict[str, Any] | None:
        """Full product detail: price, barcode, breadcrumb, size.

        @param product_id: bare SKU
        @returns parsed detail, or None on any error
        """
        try:
            async with self._client() as client:
                return await self._detail(client, product_id)
        except Exception:  # noqa: BLE001
            return None

    async def get_breadcrumb(self, sku: str) -> dict[str, Any] | None:
        """Department/aisle/shelf for a product.

        @param sku: bare SKU
        @returns breadcrumb dict, or None
        """
        detail = await self.get_product_detail(sku)
        return detail.get("breadcrumb") if detail else None

    async def search_by_barcode(self, gtin: str) -> dict[str, Any] | None:
        """Barcode lookup; Woolworths search can't match a GTIN, so always None.

        @param gtin: barcode
        @returns None, which sends the resolver to its brand+size search
        """
        return None

    async def get_cart(self) -> dict[str, Any]:
        """Current cart.

        @returns the `customerCart` object
        """
        async with self._client() as client:
            data = await _gql(client, "CustomerCart", _CART)
        return data.get("customerCart") or {}

    async def add_to_cart(self, product_id: str, quantity: int) -> None:
        """Add a quantity on top of what the cart already holds for this product.

        @param product_id: bare SKU
        @param quantity: count to add
        """
        async with self._client() as client:
            await self._ensure_signed_in(client)
            detail = await self._detail(client, product_id)
            if detail is None:
                raise GraphQLError(f"Woolworths has no product {product_id}")
            if detail["kg_only"]:
                raise NotImplementedError("sold by weight only")
            variant_key = f"{product_id}-EA"
            cart = (await _gql(client, "CustomerCart", _CART)).get("customerCart") or {}
            existing = next(
                (
                    line.get("quantity") or 0
                    for line in cart.get("lineItems") or []
                    if line.get("productVariantSku") == variant_key
                ),
                0,
            )
            data = await _gql(client, "SetCartLineItemQuantity", _PUSH, {
                "input": {"cartLineItemQuantityUpdates": [
                    {"variantKey": variant_key, "quantity": existing + quantity},
                ]},
            })
        if not data.get("setCartLineItemQuantity"):
            raise GraphQLError(f"Woolworths did not accept {variant_key}")

    async def list_usual(self) -> list[dict[str, Any]]:
        """Past-order products ranked by how many orders they appear in.

        @returns up to 250 `{sku, name}` items, most-ordered first
        """
        async with self._client() as client:
            order_numbers: list[str] = []
            page = 0
            while True:
                data = await _gql(client, "Orders", _ORDERS, {
                    "input": {"pageIndex": page, "pageSize": _ORDERS_PAGE_SIZE},
                })
                orders = data.get("orders") or {}
                results = orders.get("results") or []
                order_numbers += [str(o["orderNumber"]) for o in results if o.get("orderNumber")]
                page += 1
                if not results or page >= (orders.get("totalPages") or 0):
                    break

            sem = asyncio.Semaphore(5)

            async def _items(number: str) -> list[dict[str, Any]]:
                async with sem:
                    data = await _gql(
                        client, "OrderDetails", _ORDER_DETAILS, {"orderNumber": number}
                    )
                return (data.get("order") or {}).get("lineItems") or []

            per_order = await asyncio.gather(*(_items(n) for n in order_numbers))

        freq: dict[str, int] = {}
        best: dict[str, dict[str, Any]] = {}
        for items in per_order:
            for sku in {str(i.get("productKey") or "").strip() for i in items} - {""}:
                freq[sku] = freq.get(sku, 0) + 1
            for item in items:
                sku = str(item.get("productKey") or "").strip()
                if sku and sku not in best:
                    best[sku] = {"sku": sku, "name": (item.get("product") or {}).get("name")}
        ranked = sorted(best.values(), key=lambda i: freq[i["sku"]], reverse=True)
        return ranked[:_MAX_HISTORY]

    async def get_specials(self) -> list[dict[str, Any]]:
        """Promotion products whose price is below their was price.

        @returns [{barcode, product_id, name, now_price, was_price}]; empty on any error
        """
        results: list[dict[str, Any]] = []
        try:
            async with self._client() as client:
                for page in range(_SPECIALS_MAX_PAGES):
                    data = await _gql(client, "ProductSearch", _SEARCH, {
                        "searchInput": {"byProductPromotionSpecials": {
                            "sortBy": "RELEVANCE",
                            "pageIndex": page,
                            "pageSize": _SPECIALS_PAGE_SIZE,
                        }},
                    })
                    rows = _search_rows(data)
                    if not rows:
                        break
                    for row in rows:
                        p = _parse_product(row)
                        now, was = p["price"], p["was_price"]
                        if now is None or was is None or now >= was:
                            continue
                        results.append({
                            "barcode": None,
                            "product_id": p["sku"],
                            "name": p["name"],
                            "now_price": now,
                            "was_price": was,
                        })
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Woolworths specials fetch failed: %s", err)
        return results

    async def get_timeslots(self) -> list[dict[str, Any]]:
        """Available slots, earliest first; pickup slots only when a store is set.

        @returns [{displayName, startTime, available}]
        """
        variables = {"input": {"locationId": self.store_id} if self.store_id else {}}
        async with self._client() as client:
            data = await _gql(client, "Propositions", _PROPOSITIONS, variables)
        props = (data.get("propositions") or {}).get("propositions") or []
        slots = [
            {"displayName": p.get("name"), "startTime": p.get("startTime"), "available": True}
            for p in props
            if p.get("available")
            and (not self.store_id or str(p.get("method") or "").lower() == "pickup")
        ]
        return sorted(slots, key=lambda s: s["startTime"] or "")

    async def set_store(self) -> None:
        """Put the account in pickup mode at the configured location."""
        if not self.store_id:
            return
        async with self._client() as client:
            try:
                data = await _gql(client, "SetCartShoppingMode", _SET_SHOPPING_MODE, {
                    "setCartShoppingModeInput": {
                        "shoppingMode": _PICKUP_MODE,
                        "pickupLocationId": str(self.store_id),
                    },
                })
            except GraphQLError as err:
                raise GraphQLError(
                    f"Woolworths store {self.store_id} could not be set ({err}); "
                    "re-add the integration to pick a current store"
                ) from err
        mode = (data.get("setCartShoppingMode") or {}).get("shoppingMode") or {}
        if str(mode.get("pickupLocationId")) != str(self.store_id):
            _LOGGER.warning(
                "Woolworths kept pickup location %s instead of %s; prices may be from another store",
                mode.get("pickupLocationId"), self.store_id,
            )


async def fetch_stores() -> list[dict[str, Any]]:
    """Woolworths pickup locations for the config-flow picker, no login needed.

    @returns [{id, name, address}] where id is the location id `set_store` takes
    """
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=_HEADERS,
        timeout=_TIMEOUT,
        verify=get_default_context(),
    ) as client:
        data = await _gql(client, "SearchLocations", _LOCATIONS, {"input": {}})

    stores: list[dict[str, Any]] = []
    seen: set[str] = set()
    for loc in (data.get("locations") or {}).get("locations") or []:
        loc_id = str(loc.get("id") or "")
        if not loc_id or loc_id in seen:
            continue
        seen.add(loc_id)
        address = loc.get("address") or {}
        parts = [
            (address.get("lines") or {}).get("line1"),
            (address.get("locality") or {}).get("suburb"),
        ]
        stores.append({
            "id": loc_id,
            "name": (loc.get("name") or "").strip(),
            "address": ", ".join(p.strip() for p in parts if p and p.strip()) or None,
        })
    return stores
