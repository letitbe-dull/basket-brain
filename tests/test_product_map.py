"""Barcode map: schema, seeding, and phrase learning."""

from homeassistant.core import HomeAssistant

from custom_components.basket_brain.product_map import (
    ProductMap,
    _category_from_detail,
    _category_l1,
    compute_signature,
    confident_map_match,
)

from .conftest import FakeClient

# ---------------------------------------------------------------------------
# _category_from_detail
# ---------------------------------------------------------------------------


def test_category_from_detail_woolworths() -> None:
    detail = {
        "breadcrumb": {
            "department": {"name": "Bakery"},
            "aisle": {"name": "Sliced & Packaged Bread"},
            "shelf": {"name": "White Bread"},
        }
    }
    assert _category_from_detail(detail, "woolworths") == (
        "Bakery > Sliced & Packaged Bread > White Bread"
    )


def test_category_from_detail_foodstuffs() -> None:
    detail = {
        "categoryTrees": [
            {
                "level0": "Bakery",
                "level1": "Sliced & Packaged Bread",
                "level2": "White Bread",
            }
        ]
    }
    assert _category_from_detail(detail, "paknsave") == (
        "Bakery > Sliced & Packaged Bread > White Bread"
    )


def test_category_from_detail_missing() -> None:
    assert _category_from_detail({}, "woolworths") == ""
    assert _category_from_detail({}, "paknsave") == ""


def test_category_l1() -> None:
    assert (
        _category_l1("Bakery > Sliced & Packaged Bread > White Bread") == "bakery"
    )
    assert (
        _category_l1("Fridge, Deli & Eggs > Milk > Fresh Milk")
        == "fridge, deli & eggs"
    )
    assert _category_l1("") == ""


# ---------------------------------------------------------------------------
# compute_signature
# ---------------------------------------------------------------------------


def test_compute_signature() -> None:
    # Sorted meaningful tokens (size stripped) + ":" + department. Order-
    # independent, so a reversed Woolworths name yields the same key.
    assert (
        compute_signature("Vogel's Original Toast Bread 700g", "Bakery > Sliced Bread")
        == "bread original toast vogels:bakery"
    )
    assert compute_signature("Chelsea White Sugar 1.5kg", "Pantry > Baking") == (
        "chelsea sugar white:pantry"
    )
    assert compute_signature("", "Bakery") == ""  # no tokens → no sig
    # Same words in a different order → identical signature.
    assert compute_signature("craigs fruit jam strawberry", "Pantry > Jam") == (
        compute_signature("strawberry jam fruit craigs", "Pantry > Jam")
    )


# ---------------------------------------------------------------------------
# ProductMap in-memory operations
# ---------------------------------------------------------------------------


async def test_upsert_and_get_by_barcode(hass: HomeAssistant) -> None:
    pm = ProductMap(hass, "test_entry")
    await pm.async_load()

    entry = {
        "chains": {"woolworths": "W123"},
        "name": "Vogel's Toast Bread 700g",
        "brand": "Vogel's",
        "category": "Bakery > Sliced Bread > White Bread",
        "signature": "bread:bakery",
        "phrases": [],
        "last_seen": 0.0,
        "confidence": {"woolworths": "high"},
        "frequency": 0,
    }
    pm.upsert("9415142043470", entry)

    result = pm.get_by_barcode("9415142043470")
    assert result is not None
    assert result["name"] == "Vogel's Toast Bread 700g"
    assert pm.size == 1


async def test_get_by_signature(hass: HomeAssistant) -> None:
    pm = ProductMap(hass, "test_entry")
    await pm.async_load()

    entry = {
        "chains": {},
        "name": "Chelsea White Sugar 1.5kg",
        "brand": "Chelsea",
        "category": "Pantry",
        "signature": "sugar:pantry",
        "phrases": [],
        "last_seen": 0.0,
        "confidence": {},
        "frequency": 1,
    }
    pm.upsert("1234567890123", entry)

    result = pm.get_by_signature("sugar:pantry")
    assert result is not None
    gtin, found = result
    assert gtin == "1234567890123"
    assert found["name"] == "Chelsea White Sugar 1.5kg"


async def test_get_by_signature_miss(hass: HomeAssistant) -> None:
    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    assert pm.get_by_signature("bread:bakery") is None


# ---------------------------------------------------------------------------
# match_phrase / category_for_phrase — fuzzy token lookup
# ---------------------------------------------------------------------------


def _mp_entry(name: str, category: str, frequency: int) -> dict:
    return {
        "chains": {}, "name": name, "brand": None, "category": category,
        "signature": compute_signature(name, category), "phrases": [],
        "last_seen": 0.0, "confidence": {}, "frequency": frequency,
        "size": None, "size_unit": None,
    }


async def test_match_phrase_scores_exact_over_fuzzy(hass: HomeAssistant) -> None:
    """The exact word "bananas" beats the fuzzy singular "banana", so the real
    fruit ranks first even though the drink is more frequently bought."""
    pm = ProductMap(hass, "test_mp")
    await pm.async_load()
    pm.upsert("UPGO", _mp_entry(
        "sanitarium up & go liquid breakfast banana",
        "Pantry > Cereals & Spreads > Breakfast Drinks & Snacks", 0,
    ))
    pm.upsert("BANANAS", _mp_entry(
        "fresh fruit bananas yellow loose", "Fruit & Veg > Fruit > Bananas", 8,
    ))

    matches = pm.match_phrase("bananas")

    assert [g for g, _, _ in matches][0] == "BANANAS"  # exact word wins
    assert matches[0][2] > matches[1][2]  # and outscores the look-alike


async def test_match_phrase_requires_every_word(hass: HomeAssistant) -> None:
    """A phrase word with no representative in the name excludes that entry."""
    pm = ProductMap(hass, "test_mp2")
    await pm.async_load()
    pm.upsert("CRAIGS", _mp_entry(
        "craigs fruit jam strawberry", "Pantry > Cereals & Spreads > Jam", 0,
    ))
    pm.upsert("ANATHOTH", _mp_entry(
        "anathoth blackcurrant jam", "Pantry > Cereals & Spreads > Jam", 1,
    ))

    matches = pm.match_phrase("strawberry jam")

    assert [g for g, _, _ in matches] == ["CRAIGS"]  # blackcurrant excluded
    assert pm.category_for_phrase("strawberry jam") == "pantry"


async def test_add_phrase(hass: HomeAssistant) -> None:
    pm = ProductMap(hass, "test_entry")
    await pm.async_load()

    pm.upsert("9415142043470", {
        "chains": {}, "name": "Vogel's Bread", "brand": None, "category": "Bakery",
        "signature": "bread:bakery", "phrases": [], "last_seen": 0.0,
        "confidence": {}, "frequency": 0,
    })

    pm.add_phrase("9415142043470", "Bread")
    pm.add_phrase("9415142043470", "bread")  # dup — normalised, should not add again

    entry = pm.get_by_barcode("9415142043470")
    assert entry["phrases"] == ["bread"]


async def test_add_phrase_unknown_gtin(hass: HomeAssistant) -> None:
    """add_phrase on a missing GTIN should be a no-op."""
    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    pm.add_phrase("NOPE", "bread")  # must not raise


async def test_sig_index_updates_on_sig_change(hass: HomeAssistant) -> None:
    pm = ProductMap(hass, "test_entry")
    await pm.async_load()

    pm.upsert("GTIN1", {
        "chains": {}, "name": "Old", "brand": None, "category": "X",
        "signature": "old:x", "phrases": [], "last_seen": 0.0,
        "confidence": {}, "frequency": 0,
    })
    assert pm.get_by_signature("old:x") is not None

    # Update with a different signature.
    pm.upsert("GTIN1", {
        "chains": {}, "name": "New", "brand": None, "category": "Y",
        "signature": "new:y", "phrases": [], "last_seen": 0.0,
        "confidence": {}, "frequency": 0,
    })
    assert pm.get_by_signature("old:x") is None
    assert pm.get_by_signature("new:y") is not None


async def test_roundtrip_storage(hass: HomeAssistant) -> None:
    pm = ProductMap(hass, "test_entry")
    await pm.async_load()

    pm.upsert("GTIN_PERSIST", {
        "chains": {"woolworths": "W1"}, "name": "Milk", "brand": "Anchor",
        "category": "Fridge, Deli & Eggs > Milk > Fresh Milk",
        "signature": "milk:fridge, deli & eggs", "phrases": ["milk"],
        "last_seen": 1000.0, "confidence": {"woolworths": "high"}, "frequency": 0,
    })
    await pm.async_save()

    pm2 = ProductMap(hass, "test_entry")
    await pm2.async_load()
    result = pm2.get_by_barcode("GTIN_PERSIST")
    assert result is not None
    assert result["name"] == "Milk"
    assert pm2.get_by_signature("milk:fridge, deli & eggs") is not None


# ---------------------------------------------------------------------------
# seed_from_history
# ---------------------------------------------------------------------------


async def test_seed_from_history_woolworths(hass: HomeAssistant) -> None:
    """Woolworths primary: high-confidence items; FS chain resolved via barcode."""
    gtin = "9415142043470"

    primary = FakeClient(
        usual=[{"sku": "W123", "name": "Vogel's Toast Bread 700g"}],
        detail_results={
            "W123": {
                "id": "W123",
                "name": "Vogel's Toast Bread 700g",
                "brand": "Vogel's",
                "barcode": gtin,
                "breadcrumb": {
                    "department": {"name": "Bakery"},
                    "aisle": {"name": "Sliced & Packaged Bread"},
                    "shelf": {"name": "White Bread"},
                },
            }
        },
    )
    paknsave = FakeClient(
        barcode_results={
            gtin: {"productId": "5017010-EA-000", "name": "Vogel's Bread"}
        },
    )

    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    await pm.seed_from_history(
        "woolworths", primary, {"woolworths": primary, "paknsave": paknsave}
    )

    entry = pm.get_by_barcode(gtin)
    assert entry is not None
    assert entry["name"] == "Vogel's Toast Bread 700g"
    assert entry["chains"]["woolworths"] == "W123"
    assert entry["chains"]["paknsave"] == "5017010-EA-000"
    assert entry["confidence"]["woolworths"] == "high"
    assert entry["confidence"]["paknsave"] == "high"
    assert entry["category"] == "Bakery > Sliced & Packaged Bread > White Bread"
    assert entry["signature"] == "bread toast vogels:bakery"
    assert entry["frequency"] == 0


async def test_seed_skips_items_without_barcode(hass: HomeAssistant) -> None:
    """Items where get_product_detail returns no barcode are silently skipped."""
    primary = FakeClient(
        usual=[{"sku": "NOBARCODE", "name": "Weighted Item"}],
        detail_results={
            "NOBARCODE": {
                "id": "NOBARCODE",
                "name": "Weighted Item",
                "barcode": None,
            }
        },
    )

    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    await pm.seed_from_history("woolworths", primary, {"woolworths": primary})

    assert pm.size == 0


async def test_seed_skips_items_without_product_id(hass: HomeAssistant) -> None:
    """Items with no usable product ID are skipped before the detail call."""
    primary = FakeClient(
        usual=[{"name": "Mystery Item"}],  # no sku / id / productId
    )

    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    await pm.seed_from_history("woolworths", primary, {"woolworths": primary})

    assert pm.size == 0


async def test_seed_marks_unresolved_chain_low_confidence(hass: HomeAssistant) -> None:
    """A chain that can't resolve the barcode gets confidence=low and id=None."""
    gtin = "9415142008769"

    primary = FakeClient(
        usual=[{"sku": "W456", "name": "Niche Product"}],
        detail_results={
            "W456": {
                "id": "W456", "name": "Niche Product", "brand": None,
                "barcode": gtin,
                "breadcrumb": {"department": {"name": "Pantry"}},
            }
        },
    )
    paknsave = FakeClient(barcode_results={})  # not found on PAK'nSAVE

    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    await pm.seed_from_history(
        "woolworths", primary, {"woolworths": primary, "paknsave": paknsave}
    )

    entry = pm.get_by_barcode(gtin)
    assert entry is not None
    assert entry["chains"]["paknsave"] is None
    assert entry["confidence"]["paknsave"] == "low"


async def test_seed_fs_sibling_reuses_product_id(hass: HomeAssistant) -> None:
    """PAK'nSAVE and New World share productId — only one barcode call is made."""
    gtin = "9415142043470"
    primary = FakeClient(
        usual=[{"sku": "W123", "name": "Vogel's Bread"}],
        detail_results={
            "W123": {
                "id": "W123", "name": "Vogel's Bread", "brand": "Vogel's",
                "barcode": gtin,
                "breadcrumb": {"department": {"name": "Bakery"}},
            }
        },
    )
    paknsave = FakeClient(
        barcode_results={
            gtin: {"productId": "5017010-EA-000", "name": "Vogel's Bread"}
        },
    )
    newworld = FakeClient(barcode_results={})  # should NOT be called

    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    await pm.seed_from_history(
        "woolworths",
        primary,
        {"woolworths": primary, "paknsave": paknsave, "newworld": newworld},
    )

    entry = pm.get_by_barcode(gtin)
    # newworld inherits paknsave's product_id without a separate API call
    assert entry["chains"]["newworld"] == "5017010-EA-000"
    assert entry["confidence"]["newworld"] == "high"
    assert newworld.barcode_searched == []  # no call made


# ---------------------------------------------------------------------------
# Phase 2 — size back-fill and seed size population
# ---------------------------------------------------------------------------


async def test_backfill_size_on_load(hass: HomeAssistant) -> None:
    """Entries without size/size_unit get them populated on load."""
    pm = ProductMap(hass, "test_bf")
    await pm.async_load()

    # Manually inject a legacy entry (no size fields) as if loaded from old storage.
    pm._data["LEGACYGTIN"] = {
        "chains": {}, "name": "Anchor Blue Milk 1L", "brand": "Anchor",
        "category": "Fridge", "signature": "milk:fridge", "phrases": [],
        "last_seen": 0.0, "confidence": {}, "frequency": 0,
    }
    # Save then reload to trigger back-fill via async_load.
    await pm.async_save()

    pm2 = ProductMap(hass, "test_bf")
    await pm2.async_load()

    entry = pm2.get_by_barcode("LEGACYGTIN")
    assert entry is not None
    assert entry["size"] == 1000.0
    assert entry["size_unit"] == "ml"
    # Existing fields must survive untouched.
    assert entry["phrases"] == []
    assert entry["confidence"] == {}
    assert entry["frequency"] == 0


async def test_backfill_skips_entries_with_size(hass: HomeAssistant) -> None:
    """Entries already carrying size/size_unit are left alone by back-fill."""
    pm = ProductMap(hass, "test_skip")
    await pm.async_load()

    pm._data["KNOWNGTIN"] = {
        "chains": {}, "name": "Anchor Blue Milk 2L", "brand": "Anchor",
        "category": "Fridge", "signature": "milk:fridge", "phrases": [],
        "last_seen": 0.0, "confidence": {}, "frequency": 0,
        "size": 500.0, "size_unit": "ml",  # already set — must not be overwritten
    }
    await pm.async_save()

    pm2 = ProductMap(hass, "test_skip")
    await pm2.async_load()

    entry = pm2.get_by_barcode("KNOWNGTIN")
    assert entry["size"] == 500.0
    assert entry["size_unit"] == "ml"


async def test_backfill_size_unknown_when_unparseable(hass: HomeAssistant) -> None:
    """Entries with an unparseable name get size=None, size_unit=None."""
    pm = ProductMap(hass, "test_unknown_sz")
    await pm.async_load()

    pm._data["NOGTIN"] = {
        "chains": {}, "name": "Random Item", "brand": None,
        "category": "", "signature": "", "phrases": [],
        "last_seen": 0.0, "confidence": {}, "frequency": 0,
    }
    await pm.async_save()

    pm2 = ProductMap(hass, "test_unknown_sz")
    await pm2.async_load()

    entry = pm2.get_by_barcode("NOGTIN")
    assert entry["size"] is None
    assert entry["size_unit"] is None


async def test_seed_populates_size(hass: HomeAssistant) -> None:
    """Seeded entries have size and size_unit parsed from the product detail."""
    gtin = "9415099999999"
    primary = FakeClient(
        usual=[{"sku": "W789", "name": "Anchor Blue Milk 1L"}],
        detail_results={
            "W789": {
                "id": "W789", "name": "Anchor Blue Milk 1L", "brand": "Anchor",
                "barcode": gtin,
                "breadcrumb": {"department": {"name": "Fridge"}},
            }
        },
    )

    pm = ProductMap(hass, "test_seed_sz")
    await pm.async_load()
    await pm.seed_from_history("woolworths", primary, {"woolworths": primary})

    entry = pm.get_by_barcode(gtin)
    assert entry is not None
    assert entry["size"] == 1000.0
    assert entry["size_unit"] == "ml"


async def test_seed_size_none_when_unparseable(hass: HomeAssistant) -> None:
    """Seeded entries with no size in name get size=None — must not crash."""
    gtin = "9400600000001"
    primary = FakeClient(
        usual=[{"sku": "W000", "name": "Mystery Product"}],
        detail_results={
            "W000": {
                "id": "W000", "name": "Mystery Product", "brand": None,
                "barcode": gtin,
                "breadcrumb": {"department": {"name": "General"}},
            }
        },
    )

    pm = ProductMap(hass, "test_seed_none_sz")
    await pm.async_load()
    await pm.seed_from_history("woolworths", primary, {"woolworths": primary})

    entry = pm.get_by_barcode(gtin)
    assert entry is not None
    assert entry["size"] is None
    assert entry["size_unit"] is None


async def test_seed_depth_exceeds_old_cap(hass: HomeAssistant) -> None:
    """seed_from_history processes more items than the old 50-item cap."""
    count = 60
    usual = [{"sku": f"W{i}", "name": f"Product {i}"} for i in range(count)]
    detail_results = {
        f"W{i}": {
            "id": f"W{i}", "name": f"Product {i}", "brand": None,
            "barcode": f"{i:013d}",
            "breadcrumb": {"department": {"name": "Pantry"}},
        }
        for i in range(count)
    }
    primary = FakeClient(usual=usual, detail_results=detail_results)

    pm = ProductMap(hass, "test_depth")
    await pm.async_load()
    await pm.seed_from_history("woolworths", primary, {"woolworths": primary})

    assert pm.size == count  # all 60 seeded; old cap of 50 would have missed 10


async def test_seed_preserves_learned_phrases(hass: HomeAssistant) -> None:
    """Re-seeding must not wipe phrases learned between runs."""
    gtin = "9415142043470"

    pm = ProductMap(hass, "test_entry")
    await pm.async_load()
    # Simulate a previous run that had learned a phrase.
    pm._data[gtin] = {
        "chains": {"woolworths": "W123"}, "name": "Vogel's Bread",
        "brand": None, "category": "Bakery", "signature": "bread:bakery",
        "phrases": ["wholegrain bread"], "last_seen": 0.0,
        "confidence": {"woolworths": "high"}, "frequency": 0,
    }

    primary = FakeClient(
        usual=[{"sku": "W123", "name": "Vogel's Toast Bread 700g"}],
        detail_results={
            "W123": {
                "id": "W123", "name": "Vogel's Toast Bread 700g", "brand": "Vogel's",
                "barcode": gtin,
                "breadcrumb": {"department": {"name": "Bakery"}},
            }
        },
    )

    await pm.seed_from_history("woolworths", primary, {"woolworths": primary})

    entry = pm.get_by_barcode(gtin)
    assert entry["phrases"] == ["wholegrain bread"]  # preserved


# ---------------------------------------------------------------------------
# Phase 4 — confident_map_match (moved from resolver)
# ---------------------------------------------------------------------------


async def test_confident_map_match_high_score(hass: HomeAssistant) -> None:
    """A single strong match is confident."""
    pm = ProductMap(hass, "test_cmm")
    await pm.async_load()
    pm.upsert("BREAD", _mp_entry("Vogels Toast Bread 700g", "Bakery > Sliced Bread", 0))

    matches = pm.match_phrase("vogels bread")
    assert confident_map_match(matches) is True


async def test_confident_map_match_low_score(hass: HomeAssistant) -> None:
    """A weak match (below _MAP_HIGH) is not confident."""
    pm = ProductMap(hass, "test_cmm_low")
    await pm.async_load()
    # "biscuit" doesn't score well against a barely-related name.
    pm.upsert("MISC", _mp_entry("something unrelated biscuit", "Bakery", 0))

    matches = pm.match_phrase("chocolate biscuit")
    # Either no matches or low score — confident_map_match must be False.
    assert confident_map_match(matches) is False


async def test_confident_map_match_category_breaks_tie(hass: HomeAssistant) -> None:
    """A typed word that is also a department breaks the tie.

    "cheese" matches both a Doritos (Pantry) and a cheese block (Fridge >
    Cheese). The category signal lifts the block clear of the look-alike, so
    the match is confident and the block ranks first — "cheese is cheese".
    """
    pm = ProductMap(hass, "test_cmm_tie")
    await pm.async_load()
    pm.upsert("DORITOS", _mp_entry(
        "doritos corn chips supreme cheese 90g", "Pantry > Snacks", 0,
    ))
    pm.upsert("CHEESE", _mp_entry(
        "mainland colby cheese block 500g", "Fridge > Cheese", 1,
    ))

    matches = pm.match_phrase("cheese")
    assert matches[0][0] == "CHEESE"
    assert confident_map_match(matches) is True


async def test_confident_map_match_cross_category_no_signal(
    hass: HomeAssistant,
) -> None:
    """A cross-department near-tie with no category signal stays ambiguous."""
    pm = ProductMap(hass, "test_cmm_amb")
    await pm.async_load()
    pm.upsert("A", _mp_entry("acme supreme thing", "Pantry > Snacks", 0))
    pm.upsert("B", _mp_entry("acme supreme widget", "Fridge > Dairy", 1))

    matches = pm.match_phrase("acme supreme")
    # Neither department contains a typed word — genuine tie → not confident.
    assert confident_map_match(matches) is False


# ---------------------------------------------------------------------------
# Phase 4 — scheme stamped in storage
# ---------------------------------------------------------------------------


async def test_async_save_writes_scheme(hass: HomeAssistant) -> None:
    """async_save wraps entries with the current scheme version."""
    pm = ProductMap(hass, "test_scheme_save")
    await pm.async_load()
    pm.upsert("G1", _mp_entry("Anchor Milk 1L", "Fridge", 0))
    await pm.async_save()

    # Load a second instance from the same store and confirm scheme survived.
    pm2 = ProductMap(hass, "test_scheme_save")
    await pm2.async_load()
    assert pm2.get_by_barcode("G1") is not None


async def test_stale_scheme_discards_map(hass: HomeAssistant) -> None:
    """A stored map from an older scheme is discarded entirely on load.

    Simulates upgrading from a favourites-seeded map (scheme < current): all
    entries are dropped so the background seed_from_history repopulates from
    real order history. Nothing from the old map survives.
    """
    pm = ProductMap(hass, "test_stale_scheme")
    await pm.async_load()

    # Write a map at scheme 3 (the last favourites-seeded scheme).
    old_data = {
        "scheme": 3,
        "entries": {
            "ZOMBIE": {
                "chains": {"woolworths": "ZOM"}, "name": "Taylor Farms Crunch Salad",
                "brand": "Taylor Farms", "category": "Produce",
                "signature": "crunch salad:produce", "phrases": ["crunchie"],
                "last_seen": 0.0, "confidence": {}, "frequency": 0,
                "size": None, "size_unit": None,
            }
        },
    }
    await pm._store.async_save(old_data)

    pm2 = ProductMap(hass, "test_stale_scheme")
    await pm2.async_load()

    # Old entry discarded — map is empty, ready for a clean reseed.
    assert pm2.size == 0
    assert pm2.get_by_barcode("ZOMBIE") is None
    assert pm2.get_by_phrase("crunchie") is None


async def test_current_scheme_preserves_entries(hass: HomeAssistant) -> None:
    """A map already at the current scheme loads normally — entries and phrases kept."""
    pm = ProductMap(hass, "test_cur_scheme")
    await pm.async_load()
    pm.upsert("G1", _mp_entry("Anchor Milk 1L", "Fridge", 0))
    pm.add_phrase("G1", "anchor milk")
    await pm.async_save()

    pm2 = ProductMap(hass, "test_cur_scheme")
    await pm2.async_load()

    assert pm2.get_by_barcode("G1") is not None
    assert pm2.get_by_phrase("anchor milk") is not None
