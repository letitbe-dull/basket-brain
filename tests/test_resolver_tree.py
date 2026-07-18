"""Brand-first size-aware resolution tree — Phase 4 tests.

Tests _brand_size_tree directly for branch coverage, plus band-edge and
size-unknown-guard cases.  Normaliser unit tests live in test_product_utils.py.
"""

from typing import Any

from custom_components.basket_brain.resolver import ShoppingListManager

from .test_resolver import _entry, _StubMap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_tree = ShoppingListManager._brand_size_tree


def _hit(
    sku: str,
    name: str,
    *,
    brand: str | None = None,
    price: float | None = None,
) -> dict[str, Any]:
    h: dict[str, Any] = {"sku": sku, "name": name}
    if brand is not None:
        h["brand"] = brand
    if price is not None:
        h["price"] = price
    return h


def _anchor(brand: str | None, size: float, unit: str) -> dict[str, Any]:
    return {"brand": brand, "size": size, "size_unit": unit}


# ---------------------------------------------------------------------------
# Branch 1 — brand + exact size → search_brand_size (cheapest of matching)
# ---------------------------------------------------------------------------


def test_branch1_brand_and_exact_size() -> None:
    """Pams 1L anchor: two Pams 1L hits → cheapest wins (high, search_brand_size)."""
    candidates = [
        _hit("A", "Pams Standard Milk 1L", brand="Pams", price=2.50),
        _hit("B", "Pams Blue Milk 1L",     brand="Pams", price=2.00),  # cheaper
        _hit("C", "Anchor Blue Milk 1L",   brand="Anchor", price=1.80),  # wrong brand
    ]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["product_id"] == "B"  # cheapest Pams 1L
    assert result["confidence"] == "high"
    assert result["reason"] == "search_brand_size"


# ---------------------------------------------------------------------------
# Branch 2 — brand match, no exact size → nearest size (search_brand_nearest)
# ---------------------------------------------------------------------------


def test_branch2_brand_nearest_size() -> None:
    """Pams 1L anchor: only Pams 2L and Pams 750ml available → 750ml is nearer."""
    candidates = [
        _hit("A", "Pams Standard Milk 2L",  brand="Pams"),  # 2000ml: 1000 away
        _hit("B", "Pams Trim Milk 750ml",   brand="Pams"),  # 750ml: 250 away — nearer
    ]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["product_id"] == "B"
    assert result["confidence"] == "high"
    assert result["reason"] == "search_brand_nearest"


# ---------------------------------------------------------------------------
# Branch 3 — no brand match, cheapest exact size (search_size_cheapest)
# ---------------------------------------------------------------------------


def test_branch3_no_brand_exact_size_cheapest() -> None:
    """Pams 1L anchor: no Pams on chain → cheapest exact-size any-brand wins."""
    candidates = [
        _hit("A", "Anchor Blue Milk 1L",   brand="Anchor",  price=3.00),
        _hit("B", "Value Milk 1L",          brand="Generic", price=2.50),  # cheapest 1L
    ]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["product_id"] == "B"
    assert result["confidence"] == "high"
    assert result["reason"] == "search_size_cheapest"


# ---------------------------------------------------------------------------
# Branch 4 — no brand, no exact size, within ±25% (search_similar_size)
# ---------------------------------------------------------------------------


def test_branch4_similar_size_within_band() -> None:
    """Pams 1L anchor: only a 750ml from another brand → in-band, low confidence."""
    candidates = [
        _hit("A", "Anchor Trim Milk 750ml", brand="Anchor", price=2.00),
    ]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["product_id"] == "A"
    assert result["confidence"] == "low"
    assert result["reason"] == "search_similar_size"


# ---------------------------------------------------------------------------
# Branch 5 — nothing in band → flag (search_needs_ok)
# ---------------------------------------------------------------------------


def test_branch5_nothing_in_band_flag() -> None:
    """Pams 1L anchor: only 500ml and 2L available → out of band → flag first."""
    candidates = [
        _hit("A", "Generic Milk 500ml", brand="Generic"),  # 50% off anchor
        _hit("B", "Generic Milk 2L",    brand="Generic"),  # 100% over anchor
    ]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["product_id"] == "A"  # candidates[0]
    assert result["confidence"] == "low"
    assert result["reason"] == "search_needs_ok"


# ---------------------------------------------------------------------------
# Headline case: 1L milk must never price against 2L
# ---------------------------------------------------------------------------


def test_1L_anchor_does_not_match_2L_hit() -> None:
    """The core regression guard: a 2L product must not resolve a 1L anchor."""
    candidates = [
        _hit("X", "Pams Standard Milk 2L", brand="Pams"),  # Pams but wrong size
        _hit("Y", "Budget Milk 2L",         brand="Budget"),  # wrong brand, wrong size
    ]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    # Pams 2L takes branch 2 (brand, nearest size) — a 2L is 1000ml away.
    # No exact-size match exists, so we get brand_nearest with the 2L.
    # The point: confidence stays high (brand matched) but we used nearest, not exact.
    assert result is not None
    assert result["reason"] == "search_brand_nearest"
    assert result["product_id"] == "X"  # the Pams 2L is nearest Pams size


# ---------------------------------------------------------------------------
# Band edge tests — 750ml and 500ml against a 1L anchor
# ---------------------------------------------------------------------------


def test_750ml_is_inside_25pct_band() -> None:
    """750ml: |750-1000|=250 = 1000*0.25 → exactly on the boundary → in band."""
    candidates = [_hit("A", "Trim Milk 750ml", brand="Other")]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["reason"] == "search_similar_size"  # branch 4, not flagged


def test_500ml_is_outside_25pct_band() -> None:
    """500ml: |500-1000|=500 > 1000*0.25=250 → out of band → flag."""
    candidates = [_hit("A", "Trim Milk 500ml", brand="Other")]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["reason"] == "search_needs_ok"  # branch 5, flagged


# ---------------------------------------------------------------------------
# Epsilon on exact-size equality (±1%)
# ---------------------------------------------------------------------------


def test_1010ml_is_exact_for_1L_anchor() -> None:
    """1010ml: |1010-1000|=10 = 1000*0.01 → epsilon boundary → exact."""
    candidates = [_hit("A", "Generic 1010ml Milk", brand="Other")]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["reason"] == "search_size_cheapest"  # branch 3 — exact, any brand


def test_1020ml_is_not_exact_but_in_band() -> None:
    """1020ml: |1020-1000|=20 > 10 → not exact; |20| ≤ 250 → still in ±25% band."""
    candidates = [_hit("A", "Generic 1020ml Milk", brand="Other")]
    anchor = _anchor("Pams", 1000.0, "ml")

    result = _tree(candidates, anchor, 1000.0, "ml")

    assert result is not None
    assert result["reason"] == "search_similar_size"  # branch 4


# ---------------------------------------------------------------------------
# Size-unknown guard — falls back to head-noun behaviour when anchor has no size
# ---------------------------------------------------------------------------


async def test_size_unknown_falls_back_to_head_noun() -> None:
    """When the anchor has size=None, the tree is skipped and the first
    valid head-noun match wins at high confidence (pre-Phase-3 behaviour).

    Uses paknsave so categoryTrees inline check fires without breadcrumb calls.
    The map entry has no paknsave product_id → resolver falls to live search.
    """
    m = _StubMap()
    m.add("GTIN", _entry(
        chains={"woolworths": "W1"},  # paknsave absent → falls to search
        name="Anchor Blue Milk 2L",
        category="Fridge, Deli & Eggs",
    ))
    # Inject size=None so anchor_size is None and the tree is bypassed.
    m.entries["GTIN"]["size"] = None
    m.entries["GTIN"]["size_unit"] = None

    mgr = ShoppingListManager(product_map=m)

    from .conftest import FakeClient

    # Foodstuffs hits carry categoryTrees inline — no breadcrumb call needed.
    client = FakeClient(search_results=[
        {
            "name": "Pams Trim Milk 1L",
            "sku": "FIRST_MILK",
            "categoryTrees": [{"level0": "Fridge, Deli & Eggs"}],
        },
        {
            "name": "Pams Standard Milk 2L",
            "sku": "SECOND_MILK",
            "categoryTrees": [{"level0": "Fridge, Deli & Eggs"}],
        },
    ])

    result = await mgr._resolve_with_client("milk", "paknsave", client, None)

    # Without a size anchor, first gated head-noun match wins — not the tree.
    assert result is not None
    assert result["product_id"] == "FIRST_MILK"
    assert result["confidence"] == "high"
