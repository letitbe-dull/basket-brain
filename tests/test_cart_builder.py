"""Cheapest-chain choice and what actually lands in the trolley."""

import pytest

from custom_components.basket_brain.cart_builder import (
    CartBuilder,
    _first_slot_string,
    _slot_to_string,
)
from custom_components.basket_brain.coordinator import BasketBrainCoordinator
from custom_components.basket_brain.foodstuffs import FoodstuffsCookieExpiredError

from .conftest import FakeClient


async def test_pick_cheapest_ignores_none() -> None:
    builder = CartBuilder()
    totals = {"woolworths": None, "paknsave": 42.0, "newworld": 50.0}

    assert await builder.pick_cheapest_chain(totals) == "paknsave"


@pytest.mark.parametrize("totals", [{}, {"woolworths": None, "paknsave": None}])
async def test_pick_cheapest_all_unpriced(totals: dict) -> None:
    assert await CartBuilder().pick_cheapest_chain(totals) is None


async def test_out_of_stock_item_is_skipped_not_added() -> None:
    client = FakeClient()
    resolved = {
        "milk": {
            "paknsave": {"product_id": "P1", "confidence": "high", "reason": "alias"}
        }
    }
    prices = {"milk": {"paknsave": {"price_nzd": 4.0, "in_stock": False}}}

    result = await CartBuilder().build_cart(
        "paknsave", resolved, None, {"paknsave": client}, prices
    )

    assert client.added == []
    assert result["added"] == []
    assert result["skipped"] == [
        {"phrase": "milk", "product_id": "P1", "reason": "out_of_stock"}
    ]


@pytest.mark.parametrize(
    "prices",
    [
        None,
        {"milk": {"paknsave": {"price_nzd": 4.0}}},  # in_stock absent → assume yes
        {"milk": {"paknsave": {"price_nzd": 4.0, "in_stock": True}}},
    ],
)
async def test_item_is_added_when_not_known_out_of_stock(prices: dict | None) -> None:
    client = FakeClient()
    resolved = {
        "milk": {
            "paknsave": {"product_id": "P1", "confidence": "high", "reason": "alias"}
        }
    }

    result = await CartBuilder().build_cart(
        "paknsave", resolved, None, {"paknsave": client}, prices
    )

    assert client.added == [("P1", 1)]
    assert result["added"] == [
        {"phrase": "milk", "product_id": "P1", "quantity": 1}
    ]
    assert result["skipped"] == []


async def test_unresolved_phrase_is_ignored() -> None:
    client = FakeClient()
    resolved = {"kumara": {"paknsave": None}}

    result = await CartBuilder().build_cart(
        "paknsave", resolved, None, {"paknsave": client}, None
    )

    assert client.added == []
    assert result["added"] == []
    assert result["errors"] == []


async def test_not_implemented_means_compare_only() -> None:
    client = FakeClient(add_error=NotImplementedError())
    resolved = {
        "milk": {
            "paknsave": {"product_id": "P1", "confidence": "high", "reason": "alias"}
        }
    }

    result = await CartBuilder().build_cart(
        "paknsave", resolved, None, {"paknsave": client}, None
    )

    assert result["compare_only"] == [{"phrase": "milk", "product_id": "P1"}]
    assert result["errors"] == []


async def test_generic_error_recorded_and_loop_continues() -> None:
    bad = FakeClient(add_error=RuntimeError("boom"))
    resolved = {
        "milk": {
            "paknsave": {"product_id": "P1", "confidence": "high", "reason": "alias"}
        },
        "bread": {
            "paknsave": {"product_id": "P2", "confidence": "high", "reason": "alias"}
        },
    }

    result = await CartBuilder().build_cart(
        "paknsave", resolved, None, {"paknsave": bad}, None
    )

    assert result["added"] == []
    assert [e["phrase"] for e in result["errors"]] == ["milk", "bread"]


async def test_expired_cookies_propagate() -> None:
    client = FakeClient(
        add_error=FoodstuffsCookieExpiredError("dead", banner="paknsave")
    )
    resolved = {
        "milk": {
            "paknsave": {"product_id": "P1", "confidence": "high", "reason": "alias"}
        }
    }

    with pytest.raises(FoodstuffsCookieExpiredError):
        await CartBuilder().build_cart(
            "paknsave", resolved, None, {"paknsave": client}, None
        )


async def test_missing_client_is_an_error() -> None:
    result = await CartBuilder().build_cart(
        "paknsave",
        {
            "milk": {
                "paknsave": {
                    "product_id": "P1",
                    "confidence": "high",
                    "reason": "alias",
                }
            }
        },
        None,
        {},
        None,
    )

    assert result["added"] == []
    assert len(result["errors"]) == 1


def test_slot_skips_unavailable() -> None:
    slots = {
        "slots": [
            {"available": False, "displayName": "Tue 9am"},
            {"available": True, "displayName": "Tue 11am"},
        ]
    }

    assert _first_slot_string(slots) == "Tue 11am"


def test_slot_all_unavailable() -> None:
    assert _first_slot_string([{"isAvailable": False, "label": "Tue"}]) is None
    assert _first_slot_string([]) is None
    assert _slot_to_string({}) is None


class _QuantityStub:
    """Enough coordinator for `_compute_basket_totals` — quantities only."""

    quantity_for = BasketBrainCoordinator.quantity_for

    def __init__(self, quantities: dict[str, int] | None = None) -> None:
        self.quantities = quantities or {}


def test_basket_totals_ignore_unpriced_items() -> None:
    prices = {
        "milk": {"woolworths": {"price_nzd": 4.0}, "paknsave": {"price_nzd": 3.5}},
        "bread": {"woolworths": {"price_nzd": None}, "paknsave": None},
    }

    totals = BasketBrainCoordinator._compute_basket_totals(_QuantityStub(), prices)

    assert totals == {"woolworths": 4.0, "paknsave": 3.5}


def test_partial_basket_can_win_on_price() -> None:
    """A chain missing an item is cheaper by omission. Documented, not fixed."""
    prices = {
        "milk": {"woolworths": {"price_nzd": 4.0}, "paknsave": {"price_nzd": 3.5}},
        "steak": {"woolworths": {"price_nzd": 20.0}, "paknsave": None},
    }

    totals = BasketBrainCoordinator._compute_basket_totals(_QuantityStub(), prices)

    assert totals == {"woolworths": 24.0, "paknsave": 3.5}
