"""Specials feed: the relevantOffers path, price inference, and GTIN fallback."""

import base64
import json
import logging
from typing import Any

import httpx
import pytest
import respx
from homeassistant.core import HomeAssistant

from custom_components.basket_brain.const import CHAIN_PAKNSAVE
from custom_components.basket_brain.coordinator import BasketBrainCoordinator
from custom_components.basket_brain.foodstuffs import FoodstuffsClient, _offer_prices
from custom_components.basket_brain.product_map import ProductMap

TOKEN_URL = "https://www.paknsave.co.nz/api/user/get-current-user"
API = "https://api-prod.paknsave.co.nz"
OFFERS_URL = f"{API}/v1/edge/product/relevantOffers"
PROMOS_URL = f"{API}/v1/edge/product/personalisedPromotions"


def _token_response() -> httpx.Response:
    claims = base64.urlsafe_b64encode(
        json.dumps({"roles": ["SHOPPER"]}).encode()
    ).decode().rstrip("=")
    return httpx.Response(200, json={"access_token": f"header.{claims}.sig"})


def _offer(**overrides: Any) -> dict[str, Any]:
    base = {
        "productId": "5011106-EA-000",
        "name": "Chop Chop Chicken 85g",
        "singlePrice": {"price": 199},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _offer_prices — relevantOffers carries no wasPrice field.
# ---------------------------------------------------------------------------


def test_offer_prices_no_promotions() -> None:
    assert _offer_prices(199, None) == (199, None)
    assert _offer_prices(199, []) == (199, None)


def test_offer_prices_new_price_becomes_now_and_shelf_becomes_was() -> None:
    promos = [{"rewardType": "NEW_PRICE", "rewardValue": 149}]
    assert _offer_prices(199, promos) == (149, 199)


def test_offer_prices_ignores_non_new_price_reward_types() -> None:
    """rewardValue is a discount, not a price, for every other reward type —
    inferring a was-price from it would invent savings."""
    promos = [{"rewardType": "AMOUNT_OFF_PER_ITEM", "rewardValue": 50}]
    assert _offer_prices(199, promos) == (199, None)


def test_offer_prices_prefers_the_best_promotion() -> None:
    promos = [
        {"rewardType": "AMOUNT_OFF_PER_ITEM", "rewardValue": 50},
        {"rewardType": "NEW_PRICE", "rewardValue": 149, "bestPromotion": True},
    ]
    assert _offer_prices(199, promos) == (149, 199)


def test_offer_prices_rejects_a_new_price_above_the_shelf_price() -> None:
    promos = [{"rewardType": "NEW_PRICE", "rewardValue": 250}]
    assert _offer_prices(199, promos) == (199, None)


def test_offer_prices_survives_missing_or_junk_values() -> None:
    assert _offer_prices(None, [{"rewardType": "NEW_PRICE", "rewardValue": 149}]) == (
        None,
        None,
    )
    assert _offer_prices(199, [{"rewardType": "NEW_PRICE"}]) == (199, None)
    assert _offer_prices(199, ["not-a-dict"]) == (199, None)
    assert _offer_prices(199, "garbage") == (199, None)


# ---------------------------------------------------------------------------
# FoodstuffsClient.get_specials
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_specials_hits_the_v1_edge_path() -> None:
    """Regression for #1 — the unprefixed path 404s on every tick."""
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(OFFERS_URL).mock(
        return_value=httpx.Response(200, json={"relevantOffers": []})
    )

    await FoodstuffsClient("paknsave", "1234").get_specials()

    assert route.called


@respx.mock
async def test_get_specials_reads_the_relevant_offers_key() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(OFFERS_URL).mock(
        return_value=httpx.Response(200, json={
            "header": {"customerId": "C1"},
            "relevantOffers": [
                _offer(promotions=[{"rewardType": "NEW_PRICE", "rewardValue": 149}]),
            ],
        })
    )

    results = await FoodstuffsClient("paknsave", "1234").get_specials()

    assert results == [{
        "barcode": None,
        "product_id": "5011106-EA-000",
        "name": "Chop Chop Chicken 85g",
        "now_price": 1.49,
        "was_price": 1.99,
    }]


@respx.mock
async def test_get_specials_barcode_is_always_none() -> None:
    """The feed has no GTIN — the coordinator resolves it from the map."""
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(OFFERS_URL).mock(
        return_value=httpx.Response(200, json={"relevantOffers": [_offer()]})
    )

    results = await FoodstuffsClient("paknsave", "1234").get_specials()

    assert results[0]["barcode"] is None
    assert results[0]["product_id"] == "5011106-EA-000"


@respx.mock
async def test_get_specials_falls_back_to_display_name() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(OFFERS_URL).mock(
        return_value=httpx.Response(200, json={
            "relevantOffers": [
                {"productId": "P1", "displayName": "Milk 2L", "singlePrice": {}},
            ],
        })
    )

    results = await FoodstuffsClient("paknsave", "1234").get_specials()

    assert results[0]["name"] == "Milk 2L"
    assert results[0]["now_price"] is None


@respx.mock
async def test_get_specials_dedupes_and_drops_junk_entries() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(OFFERS_URL).mock(
        return_value=httpx.Response(200, json={
            "relevantOffers": [
                _offer(productId="P1"),
                _offer(productId="P1"),
                _offer(productId=""),
                "not-a-dict",
            ],
        })
    )

    results = await FoodstuffsClient("paknsave", "1234").get_specials()

    assert [r["product_id"] for r in results] == ["P1"]


@respx.mock
async def test_get_specials_does_not_call_personalised_promotions() -> None:
    """Deferred — it returns promoIds only, so each needs a follow-up call."""
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(OFFERS_URL).mock(
        return_value=httpx.Response(200, json={"relevantOffers": []})
    )
    promos = respx.get(PROMOS_URL).mock(return_value=httpx.Response(200, json={}))

    await FoodstuffsClient("paknsave", "1234").get_specials()

    assert not promos.called


@respx.mock
async def test_get_specials_returns_empty_on_404_without_logging_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1 again: a missing offers feed is a normal account state, not a fault.
    Logging it at error made it look like the integration had died."""
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(OFFERS_URL).mock(return_value=httpx.Response(404))

    with caplog.at_level(logging.DEBUG):
        results = await FoodstuffsClient("paknsave", "1234").get_specials()

    assert results == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("relevantOffers" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_get_specials_accepts_a_bare_list_body() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(OFFERS_URL).mock(return_value=httpx.Response(200, json=[_offer()]))

    results = await FoodstuffsClient("paknsave", "1234").get_specials()

    assert [r["product_id"] for r in results] == ["5011106-EA-000"]


# ---------------------------------------------------------------------------
# Coordinator._fetch_specials_alerts — barcode resolution.
#
# Built without __init__ so the test doesn't need a config entry or the add-on;
# _fetch_specials_alerts only touches product_map and _live_clients.
# ---------------------------------------------------------------------------


class _SpecialsClient:
    def __init__(self, specials: list[dict[str, Any]]) -> None:
        self._specials = specials

    async def get_specials(self) -> list[dict[str, Any]]:
        return self._specials


def _coordinator(pm: ProductMap, specials: list[dict[str, Any]]):
    coord = object.__new__(BasketBrainCoordinator)
    coord.product_map = pm
    coord._live_clients = lambda: {CHAIN_PAKNSAVE: _SpecialsClient(specials)}
    return coord


async def _map_with(hass: HomeAssistant, gtin: str, product_id: str) -> ProductMap:
    pm = ProductMap(hass, "test_specials")
    await pm.async_load()
    pm.upsert(gtin, {
        "chains": {CHAIN_PAKNSAVE: product_id},
        "name": "Chop Chop Chicken 85g",
        "phrases": [],
        "confidence": {CHAIN_PAKNSAVE: "high"},
    })
    return pm


async def test_specials_alert_resolves_gtin_from_product_id(
    hass: HomeAssistant,
) -> None:
    pm = await _map_with(hass, "9415142043470", "P1")
    coord = _coordinator(pm, [{
        "barcode": None,
        "product_id": "P1",
        "name": "Chop Chop Chicken 85g",
        "now_price": 1.49,
        "was_price": 1.99,
    }])

    alerts = await coord._fetch_specials_alerts()

    assert len(alerts) == 1
    assert alerts[0]["barcode"] == "9415142043470"
    assert alerts[0]["product_id"] == "P1"
    assert alerts[0]["saving"] == 0.50


async def test_specials_alert_skips_a_product_id_not_in_the_map(
    hass: HomeAssistant,
) -> None:
    pm = await _map_with(hass, "9415142043470", "P1")
    coord = _coordinator(pm, [{"barcode": None, "product_id": "UNKNOWN"}])

    assert await coord._fetch_specials_alerts() == []


async def test_specials_alert_keeps_using_an_explicit_barcode(
    hass: HomeAssistant,
) -> None:
    """Woolworths specials do carry a GTIN — that path must not regress."""
    pm = await _map_with(hass, "9415142043470", "P1")
    coord = _coordinator(pm, [{
        "barcode": "9415142043470",
        "product_id": "P1",
        "name": "Chop Chop Chicken 85g",
        "now_price": 1.49,
        "was_price": None,
    }])

    alerts = await coord._fetch_specials_alerts()

    assert len(alerts) == 1
    assert alerts[0]["saving"] is None
