"""Phase 2 — hybrid semantic matching.

The semantic layer degrades to fuzzy-only when the bundled model2vec model
can't load, so tests that assert semantic behaviour are skipped in that case
(``requires_model``) while the degradation path is exercised explicitly.
"""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.basket_brain import product_map as pm_module
from custom_components.basket_brain.product_map import ProductMap
from custom_components.basket_brain.semantic import _Semantic, get_semantic

requires_model = pytest.mark.skipif(
    not get_semantic().available,
    reason="model2vec model not bundled/installed in this environment",
)


def _entry(name: str, category: str, frequency: int) -> dict:
    return {
        "chains": {}, "name": name, "brand": None, "category": category,
        "signature": "", "phrases": [], "last_seen": 0.0, "confidence": {},
        "frequency": frequency, "size": None, "size_unit": None,
    }


class _DisabledSemantic(_Semantic):
    """A semantic instance forced off — stands in for a missing model."""

    def __init__(self) -> None:
        super().__init__()
        self._loaded = True
        self._model = None


# ---------------------------------------------------------------------------
# Semantic module — the model itself
# ---------------------------------------------------------------------------


@requires_model
def test_similarity_related_beats_unrelated() -> None:
    sem = get_semantic()
    # A real crunchie is closer to the phrase than a "crunch" salad, even though
    # both share the fuzzy stem — meaning, not string overlap.
    bar = sem.similarity("crunchie", "cadbury crunchie chocolate bar")
    salad = sem.similarity("crunchie", "taylor farms crunch salad")
    assert bar is not None and salad is not None
    assert bar > salad


@requires_model
def test_similarity_cross_meaning_below_floor() -> None:
    sem = get_semantic()
    # "milk" and a strawberry jam share no meaning — well under the veto floor.
    assert sem.similarity("milk", "craigs fruit jam strawberry") < pm_module._SEM_FLOOR


@requires_model
def test_embed_is_cached() -> None:
    sem = get_semantic()
    first = sem.embed("mainland colby cheese block 500g")
    second = sem.embed("mainland colby cheese block 500g")
    # Same object back — served from cache, not re-encoded.
    assert first is second


@requires_model
def test_warm_populates_cache() -> None:
    sem = get_semantic()
    sem.warm(["fresh fruit tomatoes loose", "anchor blue milk 2l"])
    # After warming, embedding is a cache hit (identity preserved).
    a = sem.embed("fresh fruit tomatoes loose")
    b = sem.embed("fresh fruit tomatoes loose")
    assert a is b


# ---------------------------------------------------------------------------
# Graceful degradation — no model
# ---------------------------------------------------------------------------


def test_disabled_semantic_returns_none() -> None:
    sem = _DisabledSemantic()
    assert sem.available is False
    assert sem.embed("anything") is None
    assert sem.similarity("a", "b") is None
    sem.warm(["a", "b"])  # must not raise


async def test_match_phrase_degrades_to_fuzzy(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the model unavailable, match_phrase still resolves via fuzzy."""
    monkeypatch.setattr(pm_module, "get_semantic", lambda: _DisabledSemantic())
    pm = ProductMap(hass, "test_degrade")
    await pm.async_load()
    pm.upsert("BREAD", _entry("Vogels Toast Bread 700g", "Bakery > Sliced Bread", 0))

    matches = pm.match_phrase("vogels bread")
    assert [g for g, _, _ in matches] == ["BREAD"]


# ---------------------------------------------------------------------------
# match_phrase regression fixtures
# ---------------------------------------------------------------------------


async def test_crunchie_prefers_bar_over_salad(hass: HomeAssistant) -> None:
    """"crunchie" resolves to the Cadbury bar, never the "crunch" salad."""
    pm = ProductMap(hass, "test_crunchie")
    await pm.async_load()
    pm.upsert("SALAD", _entry(
        "Taylor Farms Crunch Salad Kit", "Fresh Foods > Salads", 0,
    ))
    pm.upsert("BAR", _entry(
        "Cadbury Crunchie Chocolate Bar 40g", "Confectionery > Chocolate Bars", 3,
    ))

    matches = pm.match_phrase("crunchie")
    assert matches[0][0] == "BAR"


@requires_model
async def test_cheese_semantic_breaks_flavour_tie(hass: HomeAssistant) -> None:
    """When fuzzy and category tie, cosine picks the block over cheese-corn-chips.

    Both names carry the exact word "cheese" (fuzzy 100 each) and neither
    category mentions it, so only meaning separates them. The chips are the
    most-bought (frequency 0), so a fuzzy-only sort would rank them first —
    cosine overrides that and the actual cheese wins.
    """
    pm = ProductMap(hass, "test_cheese_sem")
    await pm.async_load()
    pm.upsert("CHIPS", _entry(
        "Doritos Corn Chips Supreme Cheese 90g", "Pantry > Snacks", 0,
    ))
    pm.upsert("BLOCK", _entry(
        "Mainland Colby Cheese Block 500g", "Fridge > Dairy", 8,
    ))

    matches = pm.match_phrase("cheese")
    assert matches[0][0] == "BLOCK"


async def test_cheese_gate_excludes_plain_chips(hass: HomeAssistant) -> None:
    """"cheese" never matches corn chips that don't carry the word at all."""
    pm = ProductMap(hass, "test_cheese_gate")
    await pm.async_load()
    pm.upsert("CHIPS", _entry("Doritos Corn Chips Original 150g", "Pantry > Snacks", 0))
    pm.upsert("BLOCK", _entry("Mainland Colby Cheese Block 500g", "Fridge > Cheese", 1))

    matches = pm.match_phrase("cheese")
    assert [g for g, _, _ in matches] == ["BLOCK"]


async def test_tomatoes_plural_matches_singular(hass: HomeAssistant) -> None:
    """"tomatoes" reaches a "tomato" entry — plural handled by the fuzzy gate."""
    pm = ProductMap(hass, "test_tomato")
    await pm.async_load()
    pm.upsert(
        "TOM", _entry("Fresh Loose Tomato", "Fruit & Veg > Vegetables > Tomatoes", 0)
    )

    matches = pm.match_phrase("tomatoes")
    assert [g for g, _, _ in matches] == ["TOM"]


async def test_strawberry_jam_beats_blackcurrant(hass: HomeAssistant) -> None:
    """"strawberry jam" matches the strawberry, not the blackcurrant jam."""
    pm = ProductMap(hass, "test_jam")
    await pm.async_load()
    pm.upsert("STRAW", _entry("Craigs Fruit Jam Strawberry 500g", "Pantry > Jam", 0))
    pm.upsert("BLACK", _entry("Anathoth Blackcurrant Jam 455g", "Pantry > Jam", 1))

    matches = pm.match_phrase("strawberry jam")
    assert [g for g, _, _ in matches] == ["STRAW"]
