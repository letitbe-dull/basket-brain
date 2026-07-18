"""Tests for product_utils — size normalisation and brand extraction."""

import pytest

from custom_components.basket_brain.product_utils import (
    extract_brand,
    meaningful_tokens,
    name_satisfies,
    normalise_brand,
    parse_size,
    product_size,
    variant_markers,
)

# ---------------------------------------------------------------------------
# parse_size — unit conversions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Volume
        ("Anchor Blue Milk 1L", (1000.0, "ml")),
        ("Pams Trim Milk 2l", (2000.0, "ml")),
        ("Zymil Lactose Free Milk 1.5L", (1500.0, "ml")),
        ("Fresh Juice 750ml", (750.0, "ml")),
        ("Sparkling Water 330ML", (330.0, "ml")),
        # Weight
        ("Pams White Sugar 1kg", (1000.0, "g")),
        ("Watties Baked Beans 420g", (420.0, "g")),
        ("Pams Flour 1.5kg", (1500.0, "g")),
        ("Tasty Cheese 500G", (500.0, "g")),
        # Count
        ("Free-Range Eggs 6pk", (6.0, "each")),
        ("Burger Buns 6pack", (6.0, "each")),
        ("Croissants 4 Pack", (4.0, "each")),
        # each / ea
        ("Single Avocado each", (1.0, "each")),
        ("Loose Lemon EA", (1.0, "each")),
        # Standalone size token
        ("1L", (1000.0, "ml")),
        ("500g", (500.0, "g")),
        # Unparseable
        ("Milk", None),
        ("", None),
        ("Blue Label", None),
    ],
)
def test_parse_size(text: str, expected: tuple[float, str] | None) -> None:
    assert parse_size(text) == expected


# ---------------------------------------------------------------------------
# product_size — field priority + name fallback
# ---------------------------------------------------------------------------


def test_product_size_uses_explicit_unit_field() -> None:
    product = {"name": "Milk 2L", "unit": "1L"}
    # unit field takes priority over name
    assert product_size(product) == (1000.0, "ml")


def test_product_size_woolworths_Unit_field() -> None:
    product = {"name": "Milk", "Unit": "2L"}
    assert product_size(product) == (2000.0, "ml")


def test_product_size_woolworths_PackageType_field() -> None:
    product = {"name": "Milk", "PackageType": "750ml"}
    assert product_size(product) == (750.0, "ml")


def test_product_size_falls_back_to_name() -> None:
    product = {"name": "Anchor Blue Milk 1L"}
    assert product_size(product) == (1000.0, "ml")


def test_product_size_returns_none_when_unparseable() -> None:
    product = {"name": "Milk"}
    assert product_size(product) is None


def test_product_size_returns_none_for_empty_product() -> None:
    assert product_size({}) is None


# ---------------------------------------------------------------------------
# normalise_brand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Anchor", "anchor"),
        ("  Pams  ", "pams"),
        ("Wattie's", "watties"),
        ("PUHOI VALLEY", "puhoi valley"),
        ("Lewis Road Creamery", "lewis road creamery"),
    ],
)
def test_normalise_brand(raw: str, expected: str) -> None:
    assert normalise_brand(raw) == expected


# ---------------------------------------------------------------------------
# extract_brand — field priority + fallback
# ---------------------------------------------------------------------------


def test_extract_brand_uses_brand_field() -> None:
    product = {"brand": "Anchor", "name": "Anchor Blue Milk 1L"}
    assert extract_brand(product) == "anchor"


def test_extract_brand_foodstuffs_brand_field() -> None:
    product = {"brand": "Wattie's", "name": "Wattie's Baked Beans 420g"}
    assert extract_brand(product) == "watties"


def test_extract_brand_falls_back_to_name_prefix() -> None:
    product = {"name": "Pams Standard Milk 1L"}
    assert extract_brand(product) == "pams"


def test_extract_brand_returns_none_for_empty_product() -> None:
    assert extract_brand({}) is None


def test_extract_brand_returns_none_for_empty_name() -> None:
    assert extract_brand({"name": ""}) is None


def test_extract_brand_ignores_empty_brand_field() -> None:
    # Empty string brand → fall back to name prefix
    product = {"brand": "", "name": "Pams White Sugar 1kg"}
    assert extract_brand(product) == "pams"


# ---------------------------------------------------------------------------
# 1L vs 2L size band check (the headline milk case)
# ---------------------------------------------------------------------------


def test_1L_and_2L_are_distinct_sizes() -> None:
    """A 1L and 2L are different normalised sizes — the core correctness check."""
    size_1l = parse_size("Pams Standard Milk 1L")
    size_2l = parse_size("Pams Standard Milk 2L")
    assert size_1l is not None
    assert size_2l is not None
    assert size_1l != size_2l
    assert size_1l == (1000.0, "ml")
    assert size_2l == (2000.0, "ml")


# ---------------------------------------------------------------------------
# meaningful_tokens — packaging / size / quantity stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4 Bananas", ["bananas"]),  # bare quantity stripped
        ("Doritos Corn Chips Supreme Cheese 90g",
         ["doritos", "corn", "chips", "supreme", "cheese"]),  # size stripped
        ("Free-Range Eggs 6pk", ["free", "range", "eggs"]),  # 6pk stripped
        ("Anchor Blue Milk 2L Bottle", ["anchor", "blue", "milk"]),  # size+pkg
        ("craigs fruit jam strawberry",
         ["craigs", "fruit", "jam", "strawberry"]),
        ("2kg", []),  # pure size → nothing
        ("", []),
    ],
)
def test_meaningful_tokens(text: str, expected: list[str]) -> None:
    assert meaningful_tokens(text) == expected


# ---------------------------------------------------------------------------
# name_satisfies — every typed word must be represented
# ---------------------------------------------------------------------------


def test_name_satisfies_multi_word_reaches_reversed_name() -> None:
    """"strawberry jam" must reach "craigs fruit jam strawberry" — both words
    are present even though the descriptor sits last (the Woolworths ordering)."""
    assert name_satisfies("strawberry jam", "craigs fruit jam strawberry") is not None


def test_name_satisfies_missing_word_is_rejected() -> None:
    """A word with no representative in the name → None (hard gate)."""
    assert name_satisfies("milk", "craigs fruit jam strawberry") is None


def test_name_satisfies_quantity_prefix_ignored() -> None:
    assert name_satisfies("4 bananas", "fresh fruit bananas yellow loose") is not None


def test_name_satisfies_exact_word_outscores_fuzzy() -> None:
    """Exact "bananas" beats the fuzzy singular "banana" — this is what lets the
    real fruit outrank the "up & go ... banana" drink."""
    exact = name_satisfies("bananas", "fresh fruit bananas yellow loose")
    fuzzy = name_satisfies("bananas", "sanitarium up & go liquid breakfast banana")
    assert exact is not None
    assert fuzzy is not None
    assert exact > fuzzy


def test_name_satisfies_empty_inputs_are_none() -> None:
    assert name_satisfies("", "anything at all") is None
    assert name_satisfies("milk", "") is None


# ---------------------------------------------------------------------------
# variant_markers — the cross-chain "same kind of thing" gate
# ---------------------------------------------------------------------------

# The gate compares marker sets by equality, so these tests assert on whether
# two names would survive it together, not on the marker names themselves.


def _same_kind(anchor: str, candidate: str) -> bool:
    return variant_markers(anchor) == variant_markers(candidate)


@pytest.mark.parametrize(
    ("anchor", "candidate"),
    [
        # The reported bug: own-brand dairy has no brand match at the other
        # chains, so it fell through to "cheapest at this size" and landed on
        # soy — at high confidence.
        ("Woolworths Milk Standard Blue Top 1L", "Pams Soy Milk 1L"),
        ("Woolworths Milk Standard Blue Top 1L", "Countdown Oat Milk 1L"),
        ("Anchor Blue Milk 2L", "Vitasoy Soya Milk 2L"),
        # ... and the reverse, so asking for soy doesn't get you dairy.
        ("Pams Soy Milk 1L", "Pams Milk 1L"),
        # Fat level is its own axis.
        ("Woolworths Milk Standard Blue Top 1L", "Pams Trim Milk 1L"),
        ("Woolworths Milk Standard Blue Top 1L", "Anchor Lite Milk 1L"),
        ("Pams Trim Milk 1L", "Pams Milk 1L"),
        # Lite is not trim — different products, different fat.
        ("Anchor Lite Milk 1L", "Pams Trim Milk 1L"),
        # The original UHT case this gate was built for.
        ("Anchor Blue Milk 1L", "Anchor Blue UHT Milk 1L"),
        ("Pams Milk 1L", "Pams Milk Powder 1kg"),
    ],
)
def test_variant_gate_separates(anchor: str, candidate: str) -> None:
    assert not _same_kind(anchor, candidate)


@pytest.mark.parametrize(
    ("anchor", "candidate"),
    [
        # Chains label the same product either way — an unlabelled milk has to
        # match a "Standard" one or nothing resolves cross-chain at all.
        ("Woolworths Milk Standard Blue Top 1L", "Pams Milk 1L"),
        ("Woolworths Milk Standard Blue Top 1L", "Pams Standard Milk 1L"),
        ("Woolworths Milk Standard Blue Top 1L", "Value Blue Top Milk 1L"),
        # NZ colour conventions are synonyms, not separate levels.
        ("Pams Trim Milk 1L", "Meadow Fresh Green Top Milk 1L"),
        ("Pams Trim Milk 1L", "Woolworths Calci Trim Milk 1L"),
        # Same plant base still matches across chains.
        ("Pams Soy Milk 1L", "Vitasoy Soya Milk 1L"),
    ],
)
def test_variant_gate_allows(anchor: str, candidate: str) -> None:
    assert _same_kind(anchor, candidate)


@pytest.mark.parametrize(
    "name",
    [
        # "milk" in the name, but not milk you drink — the fat rules must not
        # fire, or these get gated against their own category on words like
        # "whole" and "standard".
        "Whittakers Milk Chocolate 250g",
        "Milk Chocolate Digestives 200g",
        "Nestle Condensed Milk 395g",
        # No milk anywhere near it.
        "Whole Chicken 1.5kg",
        "Rolled Oats 1kg",
        "Standard White Bread 700g",
    ],
)
def test_variant_gate_ignores_non_drinking_milk(name: str) -> None:
    assert "standard" not in variant_markers(name)
    assert "trim" not in variant_markers(name)


def test_variant_gate_brand_containing_a_marker_is_not_a_false_positive() -> None:
    """"Vitasoy" contains "soy" but isn't a word boundary match — it's the
    "Soya" in the product name that earns the marker, not the brand."""
    assert "soy" not in variant_markers("Vitasoy Milky Original 1L")
    assert "soy" in variant_markers("Vitasoy Soya Milk 1L")
