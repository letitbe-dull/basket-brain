"""Woolworths GraphQL client: sign-in check, pricing, search, barcodes, push, history, specials, slots, store."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from custom_components.basket_brain.woolworths import (
    BASE_URL,
    CookieExpiredError,
    GraphQLError,
    WoolworthsClient,
    _real_gtin,
    fetch_stores,
)

FIXTURES = Path(__file__).parent / "fixtures" / "woolworths_graphql"
GRAPHQL = rf"{BASE_URL}/api/graphql.*"
SIGNED_IN = {"www.woolworths.co.nz": {"__session": "S"}}


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _mock(responses: dict[str, Any]) -> tuple[respx.Route, list[dict[str, Any]]]:
    """Route every GraphQL POST by operationName; record request bodies."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        reply = responses[body["operationName"]]
        if callable(reply):
            reply = reply(body)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    return respx.post(url__regex=GRAPHQL).mock(side_effect=handler), seen


def _ops(seen: list[dict[str, Any]]) -> list[str]:
    return [b["operationName"] for b in seen]


def _variant(key: str, price: float | None = 3.6, status: str = "IN_STOCK", **extra: Any) -> dict:
    return {
        "key": key,
        "availabilityStatus": status,
        "variantPrice": {"sellingPrice": price, "wasPrice": None, "isSpecial": False},
        **extra,
    }


def _detail(sku: str, *variants: dict, **extra: Any) -> dict:
    return {"data": {"My": {"product": {
        "key": sku, "name": f"Product {sku}", "brand": "Brand", "storeId": "9250",
        "variants": list(variants), **extra,
    }}}}


ME_OK = _fixture("me-signed-in.json")
ME_GUEST = _fixture("me-no-cookies.json")


# --- 02: transport and the Me sign-in check ---------------------------------


@respx.mock
async def test_check_authed_passes_for_signed_in_jar() -> None:
    _mock({"Me": ME_OK})
    await WoolworthsClient(SIGNED_IN).check_authed()


@pytest.mark.parametrize(
    "reply",
    [
        ME_GUEST,
        {"data": None},
        {"data": {"me": None}},
        {"errors": [{"message": "no", "extensions": {"code": "BANNED_OPERATION"}}]},
        httpx.ConnectError("boom"),
    ],
    ids=["guest", "data-null", "me-null", "banned", "transport"],
)
@respx.mock
async def test_check_authed_raises_when_not_signed_in(reply: Any) -> None:
    _mock({"Me": reply})
    with pytest.raises(CookieExpiredError):
        await WoolworthsClient(SIGNED_IN).check_authed()


@respx.mock
async def test_guest_me_logs_nothing_at_warning_or_above(caplog: pytest.LogCaptureFixture) -> None:
    _mock({"Me": ME_GUEST})
    with caplog.at_level(logging.DEBUG), pytest.raises(CookieExpiredError):
        await WoolworthsClient(SIGNED_IN).check_authed()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@respx.mock
async def test_graphql_error_logs_operation_and_server_message(caplog: pytest.LogCaptureFixture) -> None:
    _mock({"ProductSearch": httpx.Response(400, json=_fixture("search-barcode-error.json"))})
    with pytest.raises(GraphQLError):
        await WoolworthsClient(SIGNED_IN).search("milk")
    warning = next(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "ProductSearch" in warning
    assert 'Cannot query field "barcode"' in warning


@respx.mock
async def test_401_raises_cookie_expired() -> None:
    _mock({"ProductSearch": httpx.Response(401)})
    with pytest.raises(CookieExpiredError):
        await WoolworthsClient(SIGNED_IN).search("milk")


@respx.mock
async def test_rotated_cookies_reach_owner() -> None:
    respx.post(url__regex=GRAPHQL).mock(return_value=httpx.Response(
        200, json=ME_OK, headers={"set-cookie": "__session=NEW; Path=/"}
    ))
    client = WoolworthsClient(SIGNED_IN)
    rotated: list[dict[str, str]] = []
    client.on_cookies_rotated = lambda _domain, jar: rotated.append(jar)

    await client.check_authed()

    assert rotated and rotated[-1]["__session"] == "NEW"


@respx.mock
async def test_no_rest_headers_and_a_browser_user_agent() -> None:
    route, _ = _mock({"Me": ME_OK})
    await WoolworthsClient({"www.woolworths.co.nz": {"XSRF-TOKEN": "T"}}).check_authed()
    headers = route.calls.last.request.headers
    for name in ("x-requested-with", "x-ui-ver", "x-xsrf-token"):
        assert name not in headers
    assert "python-httpx" not in headers["user-agent"]


def test_only_woolworths_cookies_are_kept() -> None:
    client = WoolworthsClient({
        "www.woolworths.co.nz": {"session": "S"},
        "auth.woolworths.co.nz": {"auth0": "A"},
        "login.microsoftonline.com": {"junk": "J"},
    })
    assert client._cookies == {"session": "S", "auth0": "A"}


# --- 03: pricing on product detail -----------------------------------------


@respx.mock
async def test_get_prices_reads_selling_price_in_dollars() -> None:
    _mock({"Me": ME_OK, "GetProductDetails": _detail("282848", _variant("282848-EA", 3.6))})
    result = await WoolworthsClient(SIGNED_IN).get_prices(["282848"])
    assert result["282848"]["price"] == 3.6
    assert result["282848"]["in_stock"] is True
    assert result["282848"]["name"] == "Product 282848"


@respx.mock
async def test_both_mode_product_priced_from_ea_variant() -> None:
    _mock({"Me": ME_OK, "GetProductDetails": _detail(
        "155003", _variant("155003-KG", 5.5), _variant("155003-EA", 1.1)
    )})
    result = await WoolworthsClient(SIGNED_IN).get_prices(["155003"])
    assert result["155003"]["price"] == 1.1


@respx.mock
async def test_kg_only_product_priced_per_kg() -> None:
    _mock({"Me": ME_OK, "GetProductDetails": _detail("405838", _variant("405838-KG", 7.99))})
    result = await WoolworthsClient(SIGNED_IN).get_prices(["405838"])
    assert result["405838"]["price"] == 7.99
    assert result["405838"]["kg_only"] is True


@pytest.mark.parametrize("status", ["OUT_OF_STOCK", "OutOfStock", "UNAVAILABLE", "Unavailable"])
@respx.mock
async def test_out_of_stock_statuses(status: str) -> None:
    _mock({"Me": ME_OK, "GetProductDetails": _detail("1", _variant("1-EA", 2.0, status))})
    result = await WoolworthsClient(SIGNED_IN).get_prices(["1"])
    assert result["1"]["in_stock"] is False


@pytest.mark.parametrize(
    "reply",
    [
        {"data": {"My": {"product": None}}},
        {"errors": [{"message": "bad"}]},
        _detail("1", _variant("1-EA", None)),
        _detail("1", _variant("1-EA", 0)),
    ],
    ids=["null-product", "errors", "no-price", "zero-price"],
)
@respx.mock
async def test_unpriceable_product_is_left_out(reply: dict) -> None:
    _mock({"Me": ME_OK, "GetProductDetails": reply})
    assert await WoolworthsClient(SIGNED_IN).get_prices(["1"]) == {}


@respx.mock
async def test_signed_out_raises_before_any_price() -> None:
    _, seen = _mock({"Me": ME_GUEST, "GetProductDetails": _detail("1", _variant("1-EA"))})
    with pytest.raises(CookieExpiredError):
        await WoolworthsClient().get_prices(["1"])
    assert "GetProductDetails" not in _ops(seen)


# --- 04: search, category path and size -------------------------------------


@respx.mock
async def test_search_keeps_only_product_rows() -> None:
    body = _fixture("search-milk.json")
    _mock({"ProductSearch": body})
    hits = await WoolworthsClient(SIGNED_IN).search("milk")
    expected = [r["sku"] for r in body["data"]["My"]["products"]["results"] if r.get("sku")]
    assert [h["sku"] for h in hits] == expected
    assert hits[0]["name"] == "Anchor Milk Standard Blue 1L"


@respx.mock
async def test_search_prices_rows_in_dollars() -> None:
    _mock({"ProductSearch": {"data": {"My": {"products": {"results": [
        {"__typename": "GamResultItem"},
        {"sku": "282848", "productName": "Anchor Milk 1L", "variants": [
            {"variantKey": "282848-EA", "availabilityStatus": "InStock",
             "variantPrice": {"sellingPrice": 3.6, "wasPrice": 4.0}},
        ]},
    ]}}}}})
    hits = await WoolworthsClient(SIGNED_IN).search("milk")
    assert [(h["sku"], h["price"], h["was_price"]) for h in hits] == [("282848", 3.6, 4.0)]


@respx.mock
async def test_breadcrumb_from_level_three_category() -> None:
    _mock({"GetProductDetails": _fixture("detail-category-2hop-369908.json")})
    bc = await WoolworthsClient(SIGNED_IN).get_breadcrumb("369908")
    assert [bc[k]["name"] for k in ("department", "aisle", "shelf")] == [
        "Pantry", "Cereals & Spreads", "Breakfast Drinks & Snacks",
    ]


@respx.mock
async def test_no_level_three_category_gives_no_breadcrumb() -> None:
    _mock({"GetProductDetails": _detail("1", _variant("1-EA"), category=[
        {"key": "x", "name": "Promo", "level": 0, "parent": {"key": "PG-0000", "level": 0}},
    ])})
    assert await WoolworthsClient(SIGNED_IN).get_breadcrumb("1") is None


@respx.mock
async def test_detail_size_from_volume_size() -> None:
    _mock({"GetProductDetails": _detail("282848", _variant("282848-EA", volumeSize="1L"))})
    detail = await WoolworthsClient(SIGNED_IN).get_product_detail("282848")
    assert detail["size"] == {"volumeSize": "1L"}


# --- 05: barcodes ------------------------------------------------------------


@respx.mock
async def test_detail_carries_real_barcode() -> None:
    _mock({"GetProductDetails": _detail("282848", _variant("282848-EA", barcode="94127317"))})
    detail = await WoolworthsClient(SIGNED_IN).get_product_detail("282848")
    assert detail["barcode"] == "94127317"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("94127317", "94127317"),
        ("9400597070133", "9400597070133"),
        ("09400566010085", "09400566010085"),
        ("0264595000009", None),
        ("2112345000003", None),
        ("2612345000007", None),
        ("940059707", None),
        ("94005970701", None),
        ("02345678", None),
        (None, None),
        ("abc", None),
    ],
)
def test_real_gtin_rule(value: str | None, expected: str | None) -> None:
    assert _real_gtin(value) == expected


async def test_search_by_barcode_never_matches() -> None:
    assert await WoolworthsClient(SIGNED_IN).search_by_barcode("94127317") is None


# --- 06: cart push ------------------------------------------------------------


def _push_ok(body: dict) -> dict:
    return _fixture("mutation-each-add-response.json")


@respx.mock
async def test_push_adds_on_top_of_existing_quantity() -> None:
    route, seen = _mock({
        "Me": ME_OK,
        "GetProductDetails": _detail("624761", _variant("624761-EA")),
        "CustomerCart": _fixture("cart.json"),
        "SetCartLineItemQuantity": _push_ok,
    })
    await WoolworthsClient(SIGNED_IN).add_to_cart("624761", 3)
    push = next(b for b in seen if b["operationName"] == "SetCartLineItemQuantity")
    assert push["variables"]["input"]["cartLineItemQuantityUpdates"] == [
        {"variantKey": "624761-EA", "quantity": 5}
    ]
    assert "x-xsrf-token" not in route.calls.last.request.headers


@respx.mock
async def test_push_of_new_line_starts_from_zero() -> None:
    _, seen = _mock({
        "Me": ME_OK,
        "GetProductDetails": _detail("282848", _variant("282848-EA")),
        "CustomerCart": _fixture("cart.json"),
        "SetCartLineItemQuantity": _push_ok,
    })
    await WoolworthsClient(SIGNED_IN).add_to_cart("282848", 2)
    push = next(b for b in seen if b["operationName"] == "SetCartLineItemQuantity")
    assert push["variables"]["input"]["cartLineItemQuantityUpdates"][0]["quantity"] == 2


@respx.mock
async def test_push_with_errors_raises_server_message() -> None:
    _mock({
        "Me": ME_OK,
        "GetProductDetails": _detail("282848", _variant("282848-EA")),
        "CustomerCart": _fixture("cart.json"),
        "SetCartLineItemQuantity": {"errors": [{"message": "Product not ranged"}]},
    })
    with pytest.raises(GraphQLError, match="Product not ranged"):
        await WoolworthsClient(SIGNED_IN).add_to_cart("282848", 1)


@respx.mock
async def test_kg_only_push_is_compare_only() -> None:
    _, seen = _mock({"Me": ME_OK, "GetProductDetails": _detail("405838", _variant("405838-KG"))})
    with pytest.raises(NotImplementedError):
        await WoolworthsClient(SIGNED_IN).add_to_cart("405838", 1)
    assert "SetCartLineItemQuantity" not in _ops(seen)


@respx.mock
async def test_signed_out_push_raises_cookie_expired() -> None:
    _, seen = _mock({"Me": ME_GUEST})
    with pytest.raises(CookieExpiredError):
        await WoolworthsClient().add_to_cart("282848", 1)
    assert _ops(seen) == ["Me"]


# --- 09: order history --------------------------------------------------------


@respx.mock
async def test_list_usual_pages_orders_and_ranks_by_order_count() -> None:
    pages = {
        0: {"data": {"orders": {"totalPages": 2, "results": [{"orderNumber": "A"}, {"orderNumber": "B"}]}}},
        1: {"data": {"orders": {"totalPages": 2, "results": [{"orderNumber": "C"}]}}},
    }
    items = {
        "A": ["111", "222", "222"],
        "B": ["222", "333"],
        "C": ["222", "111"],
    }

    def details(body: dict) -> dict:
        skus = items[body["variables"]["orderNumber"]]
        return {"data": {"order": {"lineItems": [
            {"productKey": s, "product": {"name": f"item {s}"}} for s in skus
        ]}}}

    _, seen = _mock({
        "Orders": lambda b: pages[b["variables"]["input"]["pageIndex"]],
        "OrderDetails": details,
    })
    usual = await WoolworthsClient(SIGNED_IN).list_usual()

    assert [u["sku"] for u in usual] == ["222", "111", "333"]
    assert usual[0] == {"sku": "222", "name": "item 222"}
    assert sorted(b["variables"]["input"]["pageIndex"] for b in seen if b["operationName"] == "Orders") == [0, 1]


@respx.mock
async def test_list_usual_with_no_orders_is_empty() -> None:
    _mock({"Orders": {"data": {"orders": {"totalPages": 0, "results": []}}}})
    assert await WoolworthsClient(SIGNED_IN).list_usual() == []


# --- 10: specials and slots -----------------------------------------------------


def _special_row(sku: str, now: float | None, was: float | None) -> dict:
    return {"sku": sku, "productName": f"Item {sku}", "variants": [
        {"variantKey": f"{sku}-EA", "variantPrice": {"sellingPrice": now, "wasPrice": was}},
    ]}


@respx.mock
async def test_specials_keep_only_real_price_drops() -> None:
    page0 = {"data": {"My": {"products": {"results": [
        _special_row("1", 2.0, 3.0),
        _special_row("2", 3.0, None),
        _special_row("3", 3.0, 3.0),
        _special_row("4", None, 3.0),
    ]}}}}
    empty = {"data": {"My": {"products": {"results": []}}}}
    _mock({"ProductSearch": lambda b: page0 if b["variables"]["searchInput"][
        "byProductPromotionSpecials"]["pageIndex"] == 0 else empty})

    specials = await WoolworthsClient(SIGNED_IN).get_specials()

    assert specials == [{
        "barcode": None, "product_id": "1", "name": "Item 1", "now_price": 2.0, "was_price": 3.0,
    }]


@respx.mock
async def test_specials_failure_is_empty() -> None:
    _mock({"ProductSearch": httpx.Response(500)})
    assert await WoolworthsClient(SIGNED_IN).get_specials() == []


@respx.mock
async def test_timeslots_are_available_pickup_slots_earliest_first() -> None:
    _, seen = _mock({"Propositions": {"data": {"propositions": {"propositions": [
        {"name": "Pickup late", "method": "pickup", "available": True, "startTime": "2026-10-02T10:00:00+13:00"},
        {"name": "Delivery", "method": "delivery", "available": True, "startTime": "2026-10-01T08:00:00+13:00"},
        {"name": "Pickup gone", "method": "pickup", "available": False, "startTime": "2026-10-01T07:00:00+13:00"},
        {"name": "Pickup soon", "method": "pickup", "available": True, "startTime": "2026-10-01T09:00:00+13:00"},
    ]}}}})
    slots = await WoolworthsClient(SIGNED_IN, store_id="9500").get_timeslots()
    assert [s["displayName"] for s in slots] == ["Pickup soon", "Pickup late"]
    assert seen[0]["variables"] == {"input": {"locationId": "9500"}}


# --- 08: store binding ------------------------------------------------------------


@respx.mock
async def test_set_store_without_store_makes_no_request() -> None:
    route, _ = _mock({})
    await WoolworthsClient(SIGNED_IN).set_store()
    assert not route.called


@respx.mock
async def test_set_store_sets_pickup_mode_at_location() -> None:
    _, seen = _mock({"SetCartShoppingMode": {"data": {"setCartShoppingMode": {
        "shoppingMode": {"mode": "Pickup", "pickupLocationId": "9500"},
    }}}})
    await WoolworthsClient(SIGNED_IN, store_id="9500").set_store()
    assert seen[0]["variables"] == {
        "setCartShoppingModeInput": {"shoppingMode": "Pickup", "pickupLocationId": "9500"}
    }


@respx.mock
async def test_set_store_warns_when_location_not_applied(caplog: pytest.LogCaptureFixture) -> None:
    _mock({"SetCartShoppingMode": {"data": {"setCartShoppingMode": {
        "shoppingMode": {"mode": "Delivery", "pickupLocationId": "9171"},
    }}}})
    await WoolworthsClient(SIGNED_IN, store_id="9500").set_store()
    assert any("9500" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


@respx.mock
async def test_set_store_rejected_says_to_repick() -> None:
    _mock({"SetCartShoppingMode": {"errors": [{"message": "Location not found"}]}})
    with pytest.raises(GraphQLError, match="pick a current store"):
        await WoolworthsClient(SIGNED_IN, store_id="1906035").set_store()


@respx.mock
async def test_fetch_stores_lists_locations() -> None:
    _mock({"SearchLocations": {"data": {"locations": {"locations": [
        {"id": "9500", "name": "Ponsonby Woolworths ", "storeId": "9500",
         "address": {"lines": {"line1": "7 College Hill"}, "locality": {"suburb": "Ponsonby"}}},
        {"id": "3873690", "name": "EXPRESS PU Spotswood", "storeId": "",
         "address": {"lines": {"line1": None}, "locality": {"suburb": None}}},
        {"id": "9500", "name": "dup", "storeId": "9500", "address": {}},
    ]}}}})
    assert await fetch_stores() == [
        {"id": "9500", "name": "Ponsonby Woolworths", "address": "7 College Hill, Ponsonby"},
        {"id": "3873690", "name": "EXPRESS PU Spotswood", "address": None},
    ]
