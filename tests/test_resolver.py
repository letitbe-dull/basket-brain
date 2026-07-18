"""Phrase → product resolution. The wrong match here buys the wrong thing."""

from typing import Any

import pytest

from custom_components.basket_brain.product_utils import _category_l1, name_satisfies
from custom_components.basket_brain.resolver import ShoppingListManager

from .conftest import FakeClient

# ---------------------------------------------------------------------------
# Minimal in-memory ProductMap stub. Only implements the methods the resolver
# calls; keeps the tests independent of Storage/hass and easy to seed.
# ---------------------------------------------------------------------------


class _StubMap:
    def __init__(self) -> None:
        # gtin → entry
        self.entries: dict[str, dict[str, Any]] = {}
        # phrase_lower → gtin
        self.phrase_index: dict[str, str] = {}
        self.added_phrases: list[tuple[str, str]] = []

    def add(self, gtin: str, entry: dict[str, Any]) -> None:
        self.entries[gtin] = entry
        for p in entry.get("phrases", []):
            self.phrase_index[p.lower()] = gtin

    def get_by_phrase(self, phrase_lower: str):
        gtin = self.phrase_index.get(phrase_lower)
        return (gtin, self.entries[gtin]) if gtin else None

    def match_phrase(self, phrase: str):
        scored = [
            (gtin, entry, score)
            for gtin, entry in self.entries.items()
            if (score := name_satisfies(phrase, entry.get("name", ""))) is not None
        ]
        scored.sort(key=lambda x: (-x[2], x[1].get("frequency", 999)))
        return scored

    def category_for_phrase(self, phrase: str):
        cats: dict[str, int] = {}
        for _, entry, _ in self.match_phrase(phrase):
            cat = _category_l1(entry.get("category", ""))
            if cat:
                cats[cat] = cats.get(cat, 0) + 1
        if not cats:
            return None
        return max(cats, key=cats.get)

    def add_phrase(self, gtin: str, phrase: str) -> None:
        self.added_phrases.append((gtin, phrase.lower()))
        entry = self.entries.get(gtin)
        if entry is not None:
            entry.setdefault("phrases", []).append(phrase.lower())
            self.phrase_index[phrase.lower()] = gtin


def _entry(
    *,
    chains: dict[str, str],
    name: str = "test product",
    signature: str = "",
    category: str = "",
    phrases: list[str] | None = None,
    frequency: int = 0,
) -> dict[str, Any]:
    return {
        "chains": chains,
        "name": name,
        "brand": None,
        "category": category,
        "signature": signature,
        "phrases": phrases or [],
        "last_seen": 0.0,
        "confidence": {c: "high" for c in chains},
        "frequency": frequency,
    }


# ---------------------------------------------------------------------------
# Tier 1: pinned alias — always wins.
# ---------------------------------------------------------------------------


async def test_alias_beats_map() -> None:
    m = _StubMap()
    m.add("GTIN", _entry(chains={"woolworths": "FROM_MAP"}, name="Anchor Blue Milk 2L",
                          phrases=["milk"]))
    mgr = ShoppingListManager(product_map=m)
    mgr.pin_alias("Milk", "woolworths", "PINNED")

    result = await mgr.resolve("milk", "woolworths", None)

    assert result == {"product_id": "PINNED", "confidence": "high", "reason": "alias"}


# ---------------------------------------------------------------------------
# Tier 2: map — phrase lookup, then fuzzy token scan.
# ---------------------------------------------------------------------------


async def test_map_phrase_hit_beats_search() -> None:
    m = _StubMap()
    m.add("GTIN", _entry(
        chains={"woolworths": "MAPPED"}, name="Anchor Blue Milk 2L",
        phrases=["milk"],
    ))
    mgr = ShoppingListManager(product_map=m)
    client = FakeClient(search_results=[{"sku": "SEARCH_HIT"}])

    result = await mgr._resolve_with_client("milk", "woolworths", client, None)

    assert result == {
        "product_id": "MAPPED",
        "confidence": "high",
        "reason": "map_phrase",
    }
    assert client.searched == []  # search must not be called


async def test_map_fuzzy_hit_and_learns_phrase() -> None:
    """A phrase not yet learned but fuzzily matching a mapped product name
    resolves via the map — and the phrase is cached so next time it's a direct
    hit. Only confident, unambiguous matches learn the phrase."""
    m = _StubMap()
    m.add("GTIN", _entry(
        chains={"paknsave": "MAPPED_PNS"}, name="Vogel's Original Toast Bread 700g",
        category="Bakery > Sliced Bread",
    ))
    mgr = ShoppingListManager(product_map=m)
    client = FakeClient(search_results=[{"sku": "SEARCH_HIT"}])

    result = await mgr._resolve_with_client("bread", "paknsave", client, None)

    assert result["product_id"] == "MAPPED_PNS"
    assert result["reason"] == "map_match"
    assert m.added_phrases == [("GTIN", "bread")]
    assert client.searched == []


async def test_map_fuzzy_prefers_most_frequent_on_a_tie() -> None:
    """Equal-scoring entries (same name) fall back to the most-bought one."""
    m = _StubMap()
    m.add("RARE", _entry(chains={"woolworths": "RARE"},
                          name="Pams White Bread", frequency=9))
    m.add("OFTEN", _entry(chains={"woolworths": "OFTEN"},
                           name="Pams White Bread", frequency=1))
    mgr = ShoppingListManager(product_map=m)

    result = await mgr.resolve("bread", "woolworths", None)

    assert result["product_id"] == "OFTEN"


async def test_map_skips_entry_without_this_chain() -> None:
    """A map entry can hold no product_id for a given chain — resolver keeps
    looking (falls through to search) instead of returning None."""
    m = _StubMap()
    m.add("GTIN", _entry(
        chains={"woolworths": "W1", "paknsave": None},  # missing on PNS
        name="Vogel's Original Toast Bread 700g",
        category="Bakery > Sliced Bread",  # teaches the gate what to expect
    ))
    mgr = ShoppingListManager(product_map=m)
    client = FakeClient(search_results=[
        {
            "name": "Vogel's Original Toast Bread 700g",
            "sku": "PNS_BREAD",
            "categoryTrees": [{"level0": "Bakery"}],
        },
    ])

    result = await mgr._resolve_with_client("bread", "paknsave", client, None)

    assert result["product_id"] == "PNS_BREAD"
    assert result["reason"] == "search_gated"
    assert client.searched == ["bread"]


# ---------------------------------------------------------------------------
# Tier 3: category-gated live search.
# ---------------------------------------------------------------------------


async def test_search_falls_through_when_map_empty() -> None:
    """No alias, no map hit → live search takes over."""
    mgr = ShoppingListManager(product_map=_StubMap())
    client = FakeClient(search_results=[{"sku": "TOP"}, {"sku": "SECOND"}])

    result = await mgr._resolve_with_client("kumara", "woolworths", client, None)

    assert result["product_id"] == "TOP"
    assert client.searched == ["kumara"]


async def test_search_skips_category_adjacent_top_hit() -> None:
    """The bug this guards: searching "bread" floats Farrah's Wraps to the top
    because wraps share the bakery aisle. The first hit that IS bread must win."""
    mgr = ShoppingListManager(product_map=_StubMap())
    client = FakeClient(
        search_results=[
            {"name": "Farrah's Wraps Premium White", "sku": "WRAP"},
            {"name": "Vogel's Original Toast Bread 700g", "sku": "LOAF"},
        ]
    )

    result = await mgr._resolve_with_client("bread", "paknsave", client, None)

    assert result["product_id"] == "LOAF"
    assert result["confidence"] == "high"
    assert result["reason"] == "search_match"


async def test_search_top_hit_fallback_is_low_confidence() -> None:
    """Nothing genuinely IS the phrase → take hits[0] but flag for approval,
    never silently. This is what stops a wrap ending up in the cart unwatched."""
    mgr = ShoppingListManager(product_map=_StubMap())
    client = FakeClient(
        search_results=[
            {"name": "Mystery Item", "sku": "TOP"},
            {"name": "Something else", "sku": "B"},
        ]
    )

    result = await mgr._resolve_with_client("bread", "paknsave", client, None)

    assert result == {
        "product_id": "TOP",
        "confidence": "low",
        "reason": "search_top_hit",
    }


async def test_category_gate_uses_foodstuffs_inline_tree() -> None:
    """When the map tells us 'bread' should be in Bakery, a head-noun-matching
    hit sitting under Wraps is rejected — the next Bakery-categorised hit wins.

    The map is set up to teach the resolver bread→Bakery (via a Woolworths
    entry) without providing a paknsave product id, so paknsave falls through
    to live search where the category gate actually runs.
    """
    m = _StubMap()
    m.add("KNOWN", _entry(
        chains={"woolworths": "W1"},  # nothing for paknsave → fall through
        name="Vogel's Sliced Bread",
        category="Bakery > Sliced Bread > White Bread",
    ))
    mgr = ShoppingListManager(product_map=m)
    client = FakeClient(
        search_results=[
            {
                "name": "Weird Wrap Bread",
                "sku": "WRAP",
                "categoryTrees": [{"level0": "Wraps"}],
            },
            {
                "name": "Vogel's Sliced Bread",
                "sku": "LOAF",
                "categoryTrees": [{"level0": "Bakery"}],
            },
        ]
    )

    result = await mgr._resolve_with_client("bread", "paknsave", client, None)

    assert result["product_id"] == "LOAF"
    assert result["reason"] == "search_gated"


async def test_category_gate_uses_woolworths_breadcrumb() -> None:
    """On Woolworths, the search hit has no inline category — resolver calls
    get_breadcrumb per candidate and gates on the department."""
    m = _StubMap()
    m.add("KNOWN", _entry(
        chains={"woolworths": None}, name="Vogel's Sliced Bread",
        category="Bakery > Sliced Bread",
    ))
    mgr = ShoppingListManager(product_map=m)

    class _WWClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(search_results=[
                {"name": "Bakery Bread", "sku": "GOOD"},
            ])
            self.breadcrumbs: dict[str, dict[str, Any]] = {
                "GOOD": {"department": {"name": "Bakery"}},
            }

        async def get_breadcrumb(self, product_id: str):
            return self.breadcrumbs.get(product_id)

    client = _WWClient()
    result = await mgr._resolve_with_client("bread", "woolworths", client, None)

    assert result["product_id"] == "GOOD"
    assert result["reason"] == "search_gated"


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------


async def test_empty_search_returns_none() -> None:
    mgr = ShoppingListManager(product_map=_StubMap())
    client = FakeClient(search_results=[])

    assert await mgr._resolve_with_client("kumara", "woolworths", client, None) is None


async def test_search_failure_returns_none_not_raises() -> None:
    mgr = ShoppingListManager(product_map=_StubMap())
    client = FakeClient(search_error=RuntimeError("api down"))

    assert await mgr._resolve_with_client("kumara", "woolworths", client, None) is None


# ---------------------------------------------------------------------------
# resolve_all shape.
# ---------------------------------------------------------------------------


async def test_resolve_all_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = ShoppingListManager(product_map=_StubMap())

    async def _no_items(_hass):
        return []

    monkeypatch.setattr(mgr, "get_list_items", _no_items)

    assert await mgr.resolve_all(None, clients={"woolworths": FakeClient()}) == {}


async def test_resolve_all_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = ShoppingListManager(product_map=_StubMap())

    async def _items(_hass):
        return ["milk", "kumara"]

    monkeypatch.setattr(mgr, "get_list_items", _items)

    clients = {
        "woolworths": FakeClient(search_results=[{"sku": "W1"}]),
        "paknsave": FakeClient(search_results=[]),
    }

    result = await mgr.resolve_all(None, clients=clients)

    # milk on woolworths → low-confidence top-hit (no map, no head-noun match)
    assert result["milk"]["woolworths"] == {
        "product_id": "W1", "confidence": "low", "reason": "search_top_hit",
    }
    assert result["milk"]["paknsave"] is None
    assert result["kumara"]["woolworths"] == {
        "product_id": "W1", "confidence": "low", "reason": "search_top_hit",
    }
    assert result["kumara"]["paknsave"] is None


# ---------------------------------------------------------------------------
# resolve_all: primary-chain barcode anchoring.
#
# A phrase with no map entry must not be guessed independently on every
# chain — that's how "crunchie" landed a different physical product on
# New World/Pak'nSave vs Woolworths. The primary chain resolves first
# (whatever tier wins); its barcode then anchors every other chain.
# ---------------------------------------------------------------------------


async def test_resolve_all_anchors_other_chains_to_primary_barcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = ShoppingListManager(product_map=_StubMap())

    async def _items(_hass):
        return ["crunchie"]

    monkeypatch.setattr(mgr, "get_list_items", _items)

    primary_client = FakeClient(
        search_results=[{"name": "Crunchie 40g", "sku": "NW_CRUNCHIE"}],
        detail_results={"NW_CRUNCHIE": {"barcode": "9310072011234"}},
    )
    other_client = FakeClient(
        barcode_results={
            "9310072011234": {"name": "Crunchie 40g", "sku": "WW_CRUNCHIE"}
        },
        # If barcode anchoring didn't happen, this top hit would win instead —
        # a *different* product, which is exactly the bug being guarded.
        search_results=[{"name": "Crunchie Ice Cream 4pk", "sku": "WW_WRONG"}],
    )
    clients = {"newworld": primary_client, "woolworths": other_client}

    result = await mgr.resolve_all(None, clients=clients, primary_chain="newworld")

    # "Crunchie 40g" token-matches "crunchie" (the size suffix strips off),
    # so the primary chain resolves at high confidence via search_match.
    assert result["crunchie"]["newworld"] == {
        "product_id": "NW_CRUNCHIE", "confidence": "high", "reason": "search_match",
    }
    assert result["crunchie"]["woolworths"] == {
        "product_id": "WW_CRUNCHIE",
        "confidence": "high",
        "reason": "barcode_cross_chain",
    }
    assert other_client.barcode_searched == ["9310072011234"]
    assert other_client.searched == []  # never fell back to its own text search


async def test_resolve_all_falls_back_when_barcode_not_stocked_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primary chain's pick has a barcode, but the other chain doesn't
    stock it (or the lookup fails) — degrade to that chain's own search
    rather than leaving it unresolved."""
    mgr = ShoppingListManager(product_map=_StubMap())

    async def _items(_hass):
        return ["crunchie"]

    monkeypatch.setattr(mgr, "get_list_items", _items)

    primary_client = FakeClient(
        search_results=[{"name": "Crunchie 40g", "sku": "NW_CRUNCHIE"}],
        detail_results={"NW_CRUNCHIE": {"barcode": "9310072011234"}},
    )
    other_client = FakeClient(
        barcode_results={"9310072011234": None},  # not stocked on this chain
        search_results=[{"name": "Best Match", "sku": "WW_FALLBACK"}],
    )
    clients = {"newworld": primary_client, "woolworths": other_client}

    result = await mgr.resolve_all(None, clients=clients, primary_chain="newworld")

    assert result["crunchie"]["woolworths"] == {
        "product_id": "WW_FALLBACK", "confidence": "low", "reason": "search_top_hit",
    }
    assert other_client.barcode_searched == ["9310072011234"]
    assert other_client.searched == ["crunchie"]


async def test_resolve_all_caps_fallback_to_low_after_barcode_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a GTIN anchor exists but doesn't match on the other chain, the live
    search fallback is capped to low confidence — even if head-noun matching would
    normally return high. A different pack size is a guess, not the same product."""
    mgr = ShoppingListManager(product_map=_StubMap())

    async def _items(_hass):
        return ["crunchie"]

    monkeypatch.setattr(mgr, "get_list_items", _items)

    primary_client = FakeClient(
        search_results=[{"name": "Crunchie 40g", "sku": "NW_CRUNCHIE"}],
        detail_results={"NW_CRUNCHIE": {"barcode": "9310072011234"}},
    )
    # Exact GTIN not stocked; search finds a token match that would
    # normally surface as search_match / high — must be capped to low.
    other_client = FakeClient(
        barcode_results={"9310072011234": None},
        search_results=[{"name": "Crunchie 50g", "sku": "WW_DIFFERENT_SIZE"}],
    )
    clients = {"newworld": primary_client, "woolworths": other_client}

    result = await mgr.resolve_all(None, clients=clients, primary_chain="newworld")

    woolworths_res = result["crunchie"]["woolworths"]
    assert woolworths_res["product_id"] == "WW_DIFFERENT_SIZE"
    assert woolworths_res["confidence"] == "low"
    assert other_client.barcode_searched == ["9310072011234"]
    assert other_client.searched == ["crunchie"]


async def test_resolve_all_without_primary_chain_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No primary_chain passed (e.g. old call sites) → every chain resolves
    independently, exactly as before. No barcode calls happen at all."""
    mgr = ShoppingListManager(product_map=_StubMap())

    async def _items(_hass):
        return ["crunchie"]

    monkeypatch.setattr(mgr, "get_list_items", _items)

    client_a = FakeClient(search_results=[{"name": "A", "sku": "A1"}])
    client_b = FakeClient(search_results=[{"name": "B", "sku": "B1"}])
    clients = {"newworld": client_a, "woolworths": client_b}

    result = await mgr.resolve_all(None, clients=clients)

    assert result["crunchie"]["newworld"]["product_id"] == "A1"
    assert result["crunchie"]["woolworths"]["product_id"] == "B1"
    assert client_a.barcode_searched == []
    assert client_b.barcode_searched == []


# ---------------------------------------------------------------------------
# Phase 3 regression fixtures — the three live failures that drove the rework.
# Names are the real (lowercased) product names captured from the live map.
# ---------------------------------------------------------------------------


async def test_cheese_single_word_is_ambiguous_never_learns_doritos() -> None:
    """The headline bug: typing "cheese" must not resolve to — or learn onto —
    the Doritos whose name happens to end "...supreme cheese". "cheese" hits a
    corn chip, a frozen meal and a bread roll across three departments, so the
    map declines (ambiguous) and live search takes over. No phrase is learned.
    """
    m = _StubMap()
    m.add("DORITOS", _entry(
        chains={"paknsave": "DOR"},
        name="doritos multipack corn chips supreme cheese 90g",
        category="Pantry > Snacks & Sweets > Chips", frequency=0,
    ))
    m.add("MACARONI", _entry(
        chains={"paknsave": "MAC"},
        name="watties frozen meal macaroni cheese",
        category="Frozen > Frozen Meals & Snacks > Frozen Dinners", frequency=1,
    ))
    m.add("ROLLS", _entry(
        chains={"paknsave": "ROLL"},
        name="woolworths bread rolls long soft cheese topped",
        category="Bakery > Bakery In Store > Loaves, Garlic & Savoury Bread",
        frequency=2,
    ))
    mgr = ShoppingListManager(product_map=m)
    # A real cheese lives in the fridge — the map's expected department is
    # Pantry/Frozen/Bakery, so it's gated out and surfaces low for approval.
    client = FakeClient(search_results=[
        {
            "name": "Mainland Tasty Cheese 500g",
            "sku": "REAL_CHEESE",
            "categoryTrees": [{"level0": "Fridge, Deli & Eggs"}],
        },
    ])

    result = await mgr._resolve_with_client("cheese", "paknsave", client, None)

    assert client.searched == ["cheese"]  # map declined, search ran
    assert m.added_phrases == []  # never cemented cheese onto a look-alike
    assert result["product_id"] != "DOR"  # never Doritos
    assert result["confidence"] == "low"  # flagged for approval


async def test_strawberry_jam_reaches_the_usual_craigs() -> None:
    """"strawberry jam" (head noun "jam") must reach the usual
    "craigs fruit jam strawberry" (filed under "strawberry" by the old scheme).
    Both typed words are present, one department → confident high, and learned.
    """
    m = _StubMap()
    m.add("CRAIGS", _entry(
        chains={"woolworths": "CRAIGS_W", "paknsave": "CRAIGS_P"},
        name="craigs fruit jam strawberry",
        category="Pantry > Cereals & Spreads > Jam",
    ))
    m.add("ANATHOTH", _entry(
        chains={"woolworths": "ANA_W"},
        name="anathoth blackcurrant jam",
        category="Pantry > Cereals & Spreads > Jam", frequency=5,
    ))
    mgr = ShoppingListManager(product_map=m)
    client = FakeClient(search_results=[{"sku": "SHOULD_NOT_SEARCH"}])

    result = await mgr._resolve_with_client(
        "strawberry jam", "woolworths", client, None
    )

    assert result == {
        "product_id": "CRAIGS_W", "confidence": "high", "reason": "map_match",
    }
    assert client.searched == []
    assert ("CRAIGS", "strawberry jam") in m.added_phrases


async def test_bananas_prefer_the_real_fruit_over_the_lookalike() -> None:
    """"4 bananas" (quantity stripped) must land on the real loose bananas, not
    the more-frequent "up & go ... banana" drink. The exact word "bananas"
    outscores the fuzzy singular "banana", so score beats frequency.
    """
    m = _StubMap()
    m.add("UPGO", _entry(
        chains={"woolworths": "UPGO_W"},
        name="sanitarium up & go liquid breakfast banana",
        category="Pantry > Cereals & Spreads > Breakfast Drinks & Snacks",
        frequency=0,  # most-bought — must NOT win on frequency alone
    ))
    m.add("BANANAS", _entry(
        chains={"woolworths": "BANANAS_W"},
        name="fresh fruit bananas yellow loose",
        category="Fruit & Veg > Fruit > Bananas", frequency=8,
    ))
    mgr = ShoppingListManager(product_map=m)

    result = await mgr.resolve("4 bananas", "woolworths", None)

    assert result["product_id"] == "BANANAS_W"
    assert result["confidence"] == "high"
    assert result["reason"] == "map_match"
