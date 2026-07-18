"""Woolworths price/stock flattening, cookie scoping and cart auth."""

import json

import httpx
import pytest
import respx

from custom_components.basket_brain.woolworths import (
    BASE_URL,
    CookieExpiredError,
    WoolworthsClient,
    _barcode_from_image_url,
    _parse_price,
)


def test_sale_price_wins() -> None:
    product = _parse_price({"price": {"salePrice": 3.5, "originalPrice": 5.0}})
    assert product["price"] == 3.5


def test_falls_back_to_original_price() -> None:
    product = _parse_price({"price": {"salePrice": None, "originalPrice": 5.0}})
    assert product["price"] == 5.0


@pytest.mark.parametrize(
    ("status", "in_stock"),
    [("Low Stock", True), ("In Stock", True), ("Out of Stock", False)],
)
def test_stock_status(status: str, in_stock: bool) -> None:
    assert _parse_price({"availabilityStatus": status})["in_stock"] is in_stock


def test_missing_status_assumed_in_stock() -> None:
    assert _parse_price({})["in_stock"] is True


# ---------------------------------------------------------------------------
# Barcode derivation. Woolworths' payloads carry no `barcode` key at all —
# confirmed against a live response — so the GTIN has to come from the
# product image filename instead. This is the cross-chain key the resolver's
# barcode-anchor feature depends on; if this silently returns None again,
# Pak'nSave/New World fall back to guessing independently (the "different
# product on each chain" bug).
# ---------------------------------------------------------------------------


def test_barcode_derived_from_image_filename() -> None:
    assert _barcode_from_image_url("9339687276806.jpg") == "9339687276806"


@pytest.mark.parametrize("ext", [".jpg", ".jpeg", ".png", ".webp", ".JPG"])
def test_barcode_strips_known_image_extensions(ext: str) -> None:
    assert _barcode_from_image_url(f"9310072011234{ext}") == "9310072011234"


def test_barcode_none_for_missing_url() -> None:
    assert _barcode_from_image_url(None) is None


def test_barcode_none_for_non_numeric_filename() -> None:
    """Placeholder/CDN images don't follow the GTIN-as-filename convention —
    must not be mistaken for a real barcode."""
    assert _barcode_from_image_url("no-image-available.jpg") is None


def test_parse_price_populates_barcode_from_image_url() -> None:
    """Real shape captured from a live Woolworths product-detail response."""
    product = _parse_price({
        "sku": "183866",
        "name": "shine 10 in 1 dishwasher tablets lemon",
        "bigImageUrl": "9339687276806.jpg",
        "smallImageUrl": "9339687276806.jpg",
    })
    assert product["barcode"] == "9339687276806"


def test_parse_price_keeps_existing_barcode_if_present() -> None:
    """Defensive: if some endpoint ever does carry a real `barcode` field,
    don't clobber it with a guess derived from the image URL."""
    product = _parse_price({
        "barcode": "REAL_BARCODE",
        "bigImageUrl": "9339687276806.jpg",
    })
    assert product["barcode"] == "REAL_BARCODE"


def test_only_woolworths_cookies_are_kept() -> None:
    client = WoolworthsClient(
        {
            "www.woolworths.co.nz": {"session": "S"},
            "assets.woolworths.co.nz": {"cdn": "C"},
            "login.microsoftonline.com": {"junk": "J"},
        }
    )

    assert client._cookies == {"session": "S", "cdn": "C"}


@respx.mock
async def test_search_unwraps_items_and_drops_non_products() -> None:
    respx.get(f"{BASE_URL}/api/v1/products").mock(
        return_value=httpx.Response(
            200,
            json={
                "products": {
                    "items": [
                        {
                            "sku": "1",
                            "type": "Product",
                            "availabilityStatus": "In Stock",
                        },
                        {"sku": "AD", "type": "PromoTile"},
                    ]
                }
            },
        )
    )

    hits = await WoolworthsClient().search("milk")

    assert [h["sku"] for h in hits] == ["1"]


@respx.mock
async def test_add_to_cart_sends_xsrf_header() -> None:
    route = respx.post(f"{BASE_URL}/api/v1/trolleys/my/items").mock(
        return_value=httpx.Response(200, json={})
    )
    client = WoolworthsClient({"www.woolworths.co.nz": {"XSRF-TOKEN": "TOK"}})

    await client.add_to_cart("123", 1)

    assert route.calls.last.request.headers["x-xsrf-token"] == "TOK"


@respx.mock
async def test_add_to_cart_401_raises_cookie_expired() -> None:
    respx.post(f"{BASE_URL}/api/v1/trolleys/my/items").mock(
        return_value=httpx.Response(401)
    )

    with pytest.raises(CookieExpiredError):
        await WoolworthsClient().add_to_cart("123", 1)


@respx.mock
async def test_list_usual_fetches_per_order_items() -> None:
    """list_usual iterates genuine per-order line items, not the aggregated feed."""
    respx.get(f"{BASE_URL}/api/v1/shoppers/my/past-orders").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"orderId": 1001}, {"orderId": 1002}], "totalItems": 2},
        )
    )
    respx.get(f"{BASE_URL}/api/v1/shoppers/my/past-orders/1001/items").mock(
        return_value=httpx.Response(
            200,
            json={"products": {"items": [
                {"sku": "A", "name": "milk", "availabilityStatus": "In Stock"},
                {"sku": "B", "name": "eggs", "availabilityStatus": "In Stock"},
            ]}},
        )
    )
    respx.get(f"{BASE_URL}/api/v1/shoppers/my/past-orders/1002/items").mock(
        return_value=httpx.Response(
            200,
            json={"products": {"items": [
                {"sku": "A", "name": "milk", "availabilityStatus": "In Stock"},
                {"sku": "C", "name": "cheese", "availabilityStatus": "In Stock"},
            ]}},
        )
    )

    items = await WoolworthsClient().list_usual()

    # milk (sku A) appears in both orders → ranked first
    assert items[0]["sku"] == "A"
    assert {i["sku"] for i in items} == {"A", "B", "C"}


@respx.mock
async def test_list_usual_paginates_order_list() -> None:
    """list_usual paginates the order list until a short (last) page."""
    import re

    pages = [
        {"items": [{"orderId": 1}, {"orderId": 2}]},  # full page (size=2)
        {"items": [{"orderId": 3}]},                    # short → last page
    ]
    call_idx = 0

    def order_list_side_effect(request):
        nonlocal call_idx
        resp = httpx.Response(200, json=pages[call_idx])
        call_idx += 1
        return resp

    respx.get(f"{BASE_URL}/api/v1/shoppers/my/past-orders").mock(
        side_effect=order_list_side_effect
    )
    respx.get(
        re.compile(rf"{re.escape(BASE_URL)}/api/v1/shoppers/my/past-orders/\d+/items")
    ).mock(return_value=httpx.Response(200, json={"products": {"items": []}}))

    await WoolworthsClient().list_usual()

    assert call_idx == 2  # two order-list pages fetched


@respx.mock
async def test_list_usual_empty_order_list_returns_empty() -> None:
    """list_usual returns empty list when account has no order history."""
    respx.get(f"{BASE_URL}/api/v1/shoppers/my/past-orders").mock(
        return_value=httpx.Response(200, json={"items": [], "totalItems": 0})
    )

    items = await WoolworthsClient().list_usual()

    assert items == []


@respx.mock
async def test_list_usual_dedupes_across_orders() -> None:
    """Same SKU in multiple orders counts once in the output."""
    respx.get(f"{BASE_URL}/api/v1/shoppers/my/past-orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"orderId": 10}, {"orderId": 11}, {"orderId": 12}],
                "totalItems": 3,
            },
        )
    )
    for order_id in [10, 11, 12]:
        respx.get(
            f"{BASE_URL}/api/v1/shoppers/my/past-orders/{order_id}/items"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"products": {"items": [
                    {"sku": "MILK", "availabilityStatus": "In Stock"}
                ]}},
            )
        )

    items = await WoolworthsClient().list_usual()

    assert len(items) == 1
    assert items[0]["sku"] == "MILK"


@respx.mock
async def test_set_store_without_store_id_makes_no_request() -> None:
    await WoolworthsClient().set_store()

    assert len(respx.calls) == 0


@respx.mock
async def test_set_store_puts_integer_address_id() -> None:
    route = respx.put(f"{BASE_URL}/api/v1/fulfilment/my/pickup-addresses").mock(
        return_value=httpx.Response(200, json={})
    )

    await WoolworthsClient(store_id="9999").set_store()

    assert json.loads(route.calls.last.request.content) == {"addressId": 9999}
