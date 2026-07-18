"""Foodstuffs auth (the anonymous-token trap) and price/stock normalisation."""

import base64
import json

import httpx
import pytest
import respx

from custom_components.basket_brain.foodstuffs import (
    FoodstuffsClient,
    FoodstuffsCookieExpiredError,
    _jwt_claims,
    _normalise_decorated,
    _normalise_product,
)

TOKEN_URL = "https://www.paknsave.co.nz/api/user/get-current-user"
API = "https://api-prod.paknsave.co.nz"


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _token_response(roles: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"access_token": _jwt({"roles": roles})})


def test_rejects_bad_banner() -> None:
    with pytest.raises(ValueError):
        FoodstuffsClient("countdown", "1234")


def test_rejects_empty_store_id() -> None:
    with pytest.raises(ValueError):
        FoodstuffsClient("paknsave", "")


def test_only_shop_domain_cookies_are_kept() -> None:
    """Club+ carries its own refresh_token. Mixing them mints an anonymous token."""
    jar = {
        "login.clubplus.co.nz": {"refresh_token": "CLUBPLUS"},
        "www.paknsave.co.nz": {"refresh_token": "SHOP", "session": "S"},
    }

    client = FoodstuffsClient("paknsave", "1234", cookies=jar)

    assert client._cookies == {"refresh_token": "SHOP", "session": "S"}


def test_update_cookies_replaces_jar_and_drops_token() -> None:
    client = FoodstuffsClient(
        "paknsave", "1234", cookies={"www.paknsave.co.nz": {"a": "1"}}
    )
    client._token = "stale"
    client._token_expiry = 1e12

    client.update_cookies(
        {
            "login.clubplus.co.nz": {"refresh_token": "CLUBPLUS"},
            "www.paknsave.co.nz": {"b": "2"},
        }
    )

    assert client._cookies == {"b": "2"}
    assert client._token is None
    assert client._token_expiry == 0.0


def test_jwt_claims_handles_stripped_padding_and_garbage() -> None:
    assert _jwt_claims(_jwt({"roles": ["SHOPPER"]})) == {"roles": ["SHOPPER"]}
    assert _jwt_claims("not-a-jwt") == {}
    assert _jwt_claims("") == {}


@respx.mock
async def test_anonymous_token_raises_cookie_expired() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response(["ANONYMOUS"]))
    client = FoodstuffsClient("paknsave", "1234")

    with pytest.raises(FoodstuffsCookieExpiredError) as err:
        await client._get_user_token()

    assert err.value.banner == "paknsave"


@respx.mock
async def test_token_is_cached() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=_token_response(["SHOPPER"]))
    client = FoodstuffsClient("paknsave", "1234")

    first = await client._get_user_token()
    second = await client._get_user_token()

    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_401_refreshes_token_once_then_succeeds() -> None:
    token_route = respx.post(TOKEN_URL).mock(return_value=_token_response(["SHOPPER"]))
    respx.get(f"{API}/v1/edge/cart").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = FoodstuffsClient("paknsave", "1234")

    assert await client.get_cart() == {"ok": True}
    assert token_route.call_count == 2


@respx.mock
async def test_second_401_raises_cookie_expired() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response(["SHOPPER"]))
    respx.get(f"{API}/v1/edge/cart").mock(return_value=httpx.Response(401))
    client = FoodstuffsClient("paknsave", "1234")

    with pytest.raises(FoodstuffsCookieExpiredError):
        await client.get_cart()


@respx.mock
async def test_set_store_hits_the_cart_store_endpoint() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response(["SHOPPER"]))
    route = respx.post(f"{API}/v1/edge/cart/store/1234").mock(
        return_value=httpx.Response(200)
    )

    await FoodstuffsClient("paknsave", "1234").set_store()

    assert route.called


def test_normalise_decorated_price_and_stock() -> None:
    result = _normalise_decorated(
        {
            "productId": "P1",
            "name": "Milk",
            "singlePrice": {"price": 349},
            "availability": ["IN_STORE", "ONLINE"],
        }
    )

    assert result == {"id": "P1", "name": "Milk", "price_nzd": 3.49, "in_stock": True}


def test_normalise_decorated_in_store_only_is_out_of_stock() -> None:
    result = _normalise_decorated(
        {"productId": "P1", "singlePrice": {"price": 100}, "availability": ["IN_STORE"]}
    )

    assert result["in_stock"] is False


def test_normalise_product_price() -> None:
    assert _normalise_product({"productId": "P1", "singlePrice": {"price": 1250}})[
        "price_nzd"
    ] == 12.50
    assert _normalise_product({"productId": "P1"})["price_nzd"] is None


# ---------------------------------------------------------------------------
# search_by_barcode — GTIN verification.
# ---------------------------------------------------------------------------

SEARCH_URL = f"{API}/v1/edge/search/paginated/products"


@respx.mock
async def test_search_by_barcode_rejects_fuzzy_non_matching_gtin() -> None:
    """Algolia returns a fuzzy hit (e.g. Crunchie Sharepack) when the GTIN
    text doesn't match any product exactly — that hit must be rejected."""
    respx.post(TOKEN_URL).mock(return_value=_token_response(["SHOPPER"]))
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={
            "products": [
                {"productId": "SHAREPACK", "name": "Crunchie Sharepack"},
            ]
        })
    )
    respx.get(f"{API}/v1/edge/store/1234/product/SHAREPACK").mock(
        return_value=httpx.Response(
            200,
            json={
                "productId": "SHAREPACK",
                "name": "Crunchie Sharepack",
                "sku": "9310072999999",
            },
        )
    )
    client = FoodstuffsClient("paknsave", "1234")

    result = await client.search_by_barcode(
        "9310072011234"
    )  # wanted bar, got sharepack

    assert result is None


@respx.mock
async def test_search_by_barcode_returns_exact_gtin_match() -> None:
    """Search hits carry no GTIN, so the match is confirmed via product detail
    (whose `sku` is the GTIN) and normalised — a zero-padded form still matches."""
    respx.post(TOKEN_URL).mock(return_value=_token_response(["SHOPPER"]))
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={
            "products": [
                {"productId": "BAR", "name": "Crunchie 40g"},
            ]
        })
    )
    respx.get(f"{API}/v1/edge/store/1234/product/BAR").mock(
        return_value=httpx.Response(200, json={
            "productId": "BAR", "name": "Crunchie 40g", "sku": "09310072011234",
        })
    )
    client = FoodstuffsClient("paknsave", "1234")

    result = await client.search_by_barcode("9310072011234")

    assert result is not None
    assert result["id"] == "BAR"
    assert result["name"] == "Crunchie 40g"


@respx.mock
async def test_search_by_barcode_returns_none_when_no_hits() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response(["SHOPPER"]))
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    client = FoodstuffsClient("paknsave", "1234")

    assert await client.search_by_barcode("9310072011234") is None
