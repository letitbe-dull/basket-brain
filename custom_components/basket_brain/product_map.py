"""Barcode-keyed cross-chain product map.

Seeded from primary-chain purchase history; resolves known items to exact
product IDs on every enabled chain without live search.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .product_utils import (
    _category_l1,
    _product_id,
    meaningful_tokens,
    name_satisfies,
    product_size,
)
from .protocols import GroceryClient
from .semantic import get_semantic

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
_MAX_SEED_ITEMS = 250

# Incremented whenever the map must be rebuilt from scratch on next load.
# Scheme < current → the stored map is discarded and re-seeded from real order
# history on the same startup. Use _migrate_scheme() only for in-place upgrades
# that don't need a full clear (phrase re-validation, signature recompute, etc.).
_SIGNATURE_SCHEME = 4

# Thresholds for confident map matching — shared with resolver so the migration
# and the live resolver use the same definition of "strong and unambiguous".
_MAP_HIGH = 90.0
_AMBIG_DELTA = 5.0

# Score boost when a typed word is also the product's department/aisle — e.g.
# "cheese" against something filed under "Fridge & Deli > Cheese". A bare word
# like "cheese" scores 100 against every product that merely contains it (a
# cheese block, mac & cheese, cheese corn chips), so name similarity alone can't
# choose. Category identity is the deciding signal: the product that IS cheese
# lifts clear of the look-alikes, and — because the boost exceeds _AMBIG_DELTA —
# the ambiguity guard then sees one unambiguous department and commits.
_CATEGORY_BONUS = 15.0

# Semantic (model2vec) cosine floor. Vetoes a candidate whose meaning is clearly
# unrelated to the phrase even though fuzzy let a word through — e.g. "milk" vs a
# jam (cos ~0.07). Deliberately low: it's a cross-meaning safety net, not a fine
# discriminator. Near-neighbours ("crunchie" vs a "crunch" salad, ~0.52 vs ~0.47)
# sit too close to separate with a floor, so cosine breaks the ranking tie instead.
_SEM_FLOOR = 0.30

# Foodstuffs banners that share the same productId.
_FS_SIBLINGS: dict[str, str] = {"paknsave": "newworld", "newworld": "paknsave"}


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------


def _category_from_detail(detail: dict[str, Any], chain: str) -> str:
    """Build a full category path string from a product detail dict.

    Woolworths detail carries a `breadcrumb` dict; Foodstuffs detail carries
    `categoryTrees`. Returns e.g. "Bakery > Sliced & Packaged Bread > White Bread".
    """
    if chain == "woolworths":
        breadcrumb = detail.get("breadcrumb") or {}
        if isinstance(breadcrumb, dict):
            parts = [
                (breadcrumb.get(level) or {}).get("name", "").strip()
                for level in ("department", "aisle", "shelf")
            ]
            return " > ".join(p for p in parts if p)
    else:
        trees = detail.get("categoryTrees") or []
        if trees and isinstance(trees, list):
            first = trees[0]
            if isinstance(first, dict):
                parts = [
                    first.get(f"level{i}", "").strip()
                    for i in range(3)
                    if first.get(f"level{i}", "").strip()
                ]
                return " > ".join(parts)
    return ""


def confident_map_match(
    matches: list[tuple[str, dict[str, Any], float]],
) -> bool:
    """True when the top map match is strong and unambiguous.

    *matches* is best-score first. A match is confident when the best score
    clears ``_MAP_HIGH`` and isn't a cross-category coincidence — several
    entries scoring almost the same but sitting in different departments means
    the typed word describes no one product, so we decline rather than learn a
    phrase onto a look-alike.
    """
    if not matches:
        return False
    best = matches[0][2]
    if best < _MAP_HIGH:
        return False
    near = [m for m in matches if best - m[2] <= _AMBIG_DELTA]
    if len(near) > 1:
        cats = {_category_l1(entry.get("category", "")) for _, entry, _ in near}
        if len(cats) > 1:
            return False
    return True


def compute_signature(name: str, category: str) -> str:
    """tokens:category_l1 — a stable identity key for a product entry.

    The meaningful words of the name (packaging/size stripped) are sorted so
    the key is order-independent, then joined and suffixed with the department:
    "Doritos Corn Chips Supreme Cheese 90g" + "Pantry > Snacks" →
    "cheese chips corn doritos supreme:pantry". Empty when the name yields no
    meaningful words.

    Lookups no longer key off this — phrase matching scores against the name
    directly (see ``ProductMap.match_phrase``) — but a distinct signature scheme
    lets the map rebuild (Phase 4) tell old entries from new ones.
    """
    tokens = sorted(meaningful_tokens(name))
    if not tokens:
        return ""
    return f"{' '.join(tokens)}:{_category_l1(category)}"


# ---------------------------------------------------------------------------
# ProductMap
# ---------------------------------------------------------------------------


class ProductMap:
    """Persisted barcode→product map, seeded from primary-chain purchase history.

    Map entry shape (per GTIN):
      {
        chains:     {chain: product_id | None},
        name:       str,
        brand:      str | None,
        category:   str,            # e.g. "Bakery > Sliced Bread > White Bread"
        signature:  str,            # head_noun:category_l1 — phrase lookup key
        phrases:    [str],          # learned from use (Phase 3)
        last_seen:  float,          # unix timestamp
        confidence: {chain: "high" | "low"},
        frequency:  int,            # rank in primary-chain history (0 = most frequent)
        size:       float | None,   # normalised pack size (ml, g, or count)
        size_unit:  str | None,     # "ml", "g", or "each"; None when unknown
      }
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.product_map.{entry_id}", private=True
        )
        self._data: dict[str, dict[str, Any]] = {}  # gtin → entry
        self._sig_index: dict[str, str] = {}  # signature → gtin
        self._phrase_index: dict[str, str] = {}  # phrase_lower → gtin

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def async_load(self) -> None:
        """Load the map from storage. Missing file → start empty.

        Handles two on-disk shapes:
          • Legacy (scheme 1): a plain ``{gtin: entry}`` dict.
          • Current: ``{"scheme": N, "entries": {gtin: entry}}``.

        A stored scheme older than ``_SIGNATURE_SCHEME`` means the data is stale
        and can't be upgraded in place — the map is cleared so the background
        ``seed_from_history`` task (always scheduled at startup) rebuilds it clean
        from real order history.
        """
        raw = await self._store.async_load() or {}
        if "entries" in raw:
            stored_scheme = int(raw.get("scheme", 1))
            self._data = raw["entries"]
        else:
            # Legacy plain-dict — treat as scheme 1.
            stored_scheme = 1
            self._data = raw

        if stored_scheme < _SIGNATURE_SCHEME:
            _LOGGER.info(
                "ProductMap: scheme %d → %d; discarding stale map — "
                "will reseed from real order history",
                stored_scheme, _SIGNATURE_SCHEME,
            )
            self._data = {}
        else:
            self._backfill_size()

        # Pre-embed map names so resolution doesn't hit a cold model on first use.
        await self._warm_semantic()

        self._rebuild_indexes()
        _LOGGER.debug("ProductMap: loaded %d entries", len(self._data))

    async def _warm_semantic(self) -> None:
        """Pre-load the model and embed every map name in the executor.

        The one-off cold-cache step (Phase 2): the ~30 MB model load and the
        batch encode both do real work, so they run off the event loop. A no-op
        when the model can't load — matching degrades to fuzzy-only.
        """
        names = [e.get("name") or "" for e in self._data.values()]
        names = [n for n in names if n]
        if not names:
            return
        await self._hass.async_add_executor_job(get_semantic().warm, names)

    def _backfill_size(self) -> None:
        """Add size/size_unit to entries that pre-date Phase 2 (size-brand plan).

        Entries that already have both fields are left untouched so we never
        overwrite a more accurate value that was set during seeding.
        """
        backfilled = 0
        for entry in self._data.values():
            if "size" in entry and "size_unit" in entry:
                continue
            name = entry.get("name") or ""
            result = product_size({"name": name}) if name else None
            entry["size"] = result[0] if result else None
            entry["size_unit"] = result[1] if result else None
            backfilled += 1
        if backfilled:
            _LOGGER.debug("ProductMap: back-filled size on %d entries", backfilled)

    async def async_save(self) -> None:
        """Persist the map to storage."""
        await self._store.async_save(
            {"scheme": _SIGNATURE_SCHEME, "entries": self._data}
        )

    def _rebuild_indexes(self) -> None:
        self._sig_index = {}
        self._phrase_index = {}
        for gtin, entry in self._data.items():
            if sig := entry.get("signature"):
                self._sig_index[sig] = gtin
            for phrase in entry.get("phrases", []):
                self._phrase_index[phrase.lower()] = gtin

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._data)

    def get_by_barcode(self, gtin: str) -> dict[str, Any] | None:
        """Return the map entry for a GTIN, or None."""
        return self._data.get(gtin)

    def get_by_signature(self, sig: str) -> tuple[str, dict[str, Any]] | None:
        """Return (gtin, entry) for a signature, or None."""
        gtin = self._sig_index.get(sig)
        if gtin:
            return gtin, self._data[gtin]
        return None

    def get_by_phrase(self, phrase_lower: str) -> tuple[str, dict[str, Any]] | None:
        """Return (gtin, entry) for a previously-learned phrase, or None."""
        gtin = self._phrase_index.get(phrase_lower)
        if gtin:
            return gtin, self._data[gtin]
        return None

    def match_phrase(
        self, phrase: str
    ) -> list[tuple[str, dict[str, Any], float]]:
        """Return every (gtin, entry, score) whose name satisfies *phrase*.

        Hybrid fuzzy + semantic matching:
          1. Cheap gate — fuzzy token matching (``name_satisfies``): every typed
             word must be represented, or the entry is dropped. Handles
             brands/typos/plurals and keeps the 0–100 score scale the resolver's
             confidence thresholds are tuned to.
          2. ``_CATEGORY_BONUS`` when a typed word is also the product's
             department, so "cheese" prefers something filed under Cheese.
          3. Semantic (model2vec) layer over the survivors: a cosine below
             ``_SEM_FLOOR`` vetoes a cross-meaning look-alike (e.g. "milk" vs a
             jam), and cosine then breaks the ranking tie so meaning — not raw
             string similarity — decides between near-equal fuzzy scores (the
             real "crunchie" bar edges out a "crunch" salad). Degrades to
             fuzzy-only when the model is unavailable.

        Ordered best fuzzy score first, cosine breaking ties, then frequency
        rank so the most-bought like-for-like wins.
        """
        phrase_tokens = set(meaningful_tokens(phrase))
        sem = get_semantic()
        # (gtin, entry, score, cosine) — cosine is -1.0 when semantics are off.
        scored: list[tuple[str, dict[str, Any], float, float]] = []
        for gtin, entry in self._data.items():
            name = entry.get("name", "")
            score = name_satisfies(phrase, name)
            if score is None:
                continue
            cos = sem.similarity(phrase, name)
            if cos is not None and cos < _SEM_FLOOR:
                continue
            if phrase_tokens & set(meaningful_tokens(entry.get("category", ""))):
                score += _CATEGORY_BONUS
            scored.append((gtin, entry, score, cos if cos is not None else -1.0))
        scored.sort(key=lambda x: (-x[2], -x[3], x[1].get("frequency", 999)))
        return [(gtin, entry, score) for gtin, entry, score, _ in scored]

    def category_for_phrase(self, phrase: str) -> str | None:
        """Return the most-common L1 category among entries matching *phrase*.

        The category hint the resolver uses to gate live-search hits — so a
        search for "bread" rejects a hit categorised under Wraps even when its
        name superficially matches. Returns None when the map has no signal.
        """
        counts: dict[str, int] = {}
        for _, entry, _ in self.match_phrase(phrase):
            cat = _category_l1(entry.get("category", ""))
            if cat:
                counts[cat] = counts.get(cat, 0) + 1
        if not counts:
            return None
        return max(counts, key=counts.get)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert(self, gtin: str, entry: dict[str, Any]) -> None:
        """Add or update an entry; keeps the indexes in sync."""
        old = self._data.get(gtin)
        if old:
            # Drop stale signature/phrases from the indexes.
            if old.get("signature") and old["signature"] != entry.get("signature"):
                self._sig_index.pop(old["signature"], None)
            new_phrases = set(entry.get("phrases", []))
            for p in old.get("phrases", []):
                if p not in new_phrases:
                    self._phrase_index.pop(p, None)

        self._data[gtin] = entry
        if sig := entry.get("signature"):
            self._sig_index[sig] = gtin
        for p in entry.get("phrases", []):
            self._phrase_index[p.lower()] = gtin

    def add_phrase(self, gtin: str, phrase: str) -> None:
        """Record a typed phrase that resolved to this GTIN (called by resolver)."""
        entry = self._data.get(gtin)
        if entry is None:
            return
        phrases = entry.setdefault("phrases", [])
        pl = phrase.lower()
        if pl not in phrases:
            phrases.append(pl)
            self._phrase_index[pl] = gtin

    def promote_confidence(self, gtin: str, chain: str) -> None:
        """Upgrade a per-chain resolution to 'high' confidence after user approval."""
        entry = self._data.get(gtin)
        if entry:
            entry.setdefault("confidence", {})[chain] = "high"

    def find_gtin_by_product_id(self, chain: str, product_id: str) -> str | None:
        """Return the GTIN whose chain entry matches this product ID, or None."""
        for gtin, entry in self._data.items():
            if str(entry.get("chains", {}).get(chain) or "") == str(product_id):
                return gtin
        return None

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    async def seed_from_history(
        self,
        primary_chain: str,
        primary_client: GroceryClient,
        all_clients: dict[str, GroceryClient],
    ) -> None:
        """Build/refresh the map from primary-chain purchase history.

        Idempotent — existing entries are updated; learned phrases are preserved.
        Caps at _MAX_SEED_ITEMS to avoid hammering the API at startup.
        """
        _LOGGER.info("ProductMap: seeding from %s history", primary_chain)

        try:
            history = await primary_client.list_usual()
        except Exception:
            _LOGGER.exception(
                "ProductMap: could not fetch history from %s", primary_chain
            )
            return

        seeded = 0
        for rank, item in enumerate(history[:_MAX_SEED_ITEMS]):
            try:
                ok = await self._seed_one(
                    rank, item, primary_chain, primary_client, all_clients
                )
                if ok:
                    seeded += 1
            except Exception:
                _LOGGER.debug(
                    "ProductMap: skipped item %r", item.get("name", "?"), exc_info=True
                )

        await self.async_save()
        _LOGGER.info(
            "ProductMap: seeded %d new/updated items (%d total)", seeded, self.size
        )

    async def _seed_one(
        self,
        rank: int,
        item: dict[str, Any],
        primary_chain: str,
        primary_client: GroceryClient,
        all_clients: dict[str, GroceryClient],
    ) -> bool:
        """Enrich one primary-chain history item and add it to the map.

        Returns True if the item was added/updated, False if it was skipped.
        """
        pid = _product_id(item)
        if not pid:
            return False

        # Items from Foodstuffs purchasedproducts/top carry the GTIN and
        # categoryTrees directly — skip the detail call for those.
        item_barcode = str(item.get("barcode") or "").strip()
        item_trees = item.get("categoryTrees") or []

        if item_barcode and item_trees:
            # Build a synthetic detail from what the item already carries.
            detail: dict[str, Any] = {
                "barcode": item_barcode,
                "name": item.get("name"),
                "brand": item.get("brand"),
                "categoryTrees": item_trees,
            }
        else:
            detail = await primary_client.get_product_detail(pid)
            if not detail:
                return False
            # If the item had a GTIN that the detail call missed, fill it in.
            if item_barcode and not detail.get("barcode"):
                detail["barcode"] = item_barcode

        gtin = str(detail.get("barcode") or "").strip()
        if not gtin:
            # Some products (weighted items, specials) have no GTIN.
            return False

        # Preserve learned phrases from any existing entry.
        existing = self._data.get(gtin, {})

        name = detail.get("name") or item.get("name") or ""
        brand = detail.get("brand") or item.get("brand")
        category = _category_from_detail(detail, primary_chain)
        sig = compute_signature(name, category)

        size_result = (
            product_size(detail)
            or (product_size({"name": name}) if name else None)
        )
        size_val = size_result[0] if size_result else None
        size_unit = size_result[1] if size_result else None

        # Primary chain is always high-confidence (we just fetched it).
        chains: dict[str, str | None] = {primary_chain: pid}
        confidence: dict[str, str] = {primary_chain: "high"}

        # Resolve every comparison chain via barcode search.
        for chain, client in all_clients.items():
            if chain == primary_chain:
                continue

            # Foodstuffs PNS and NW share the same productId — avoid a
            # redundant API call by copying the sibling's result when known.
            sibling = _FS_SIBLINGS.get(chain)
            if sibling and sibling in chains:
                chains[chain] = chains[sibling]
                confidence[chain] = confidence[sibling]
                continue

            try:
                found = await client.search_by_barcode(gtin)
            except Exception:
                found = None

            if found and (found_pid := _product_id(found)):
                chains[chain] = found_pid
                confidence[chain] = "high"
            else:
                chains[chain] = None
                confidence[chain] = "low"

        self.upsert(
            gtin,
            {
                "chains": chains,
                "name": name,
                "brand": brand,
                "category": category,
                "signature": sig,
                "phrases": existing.get("phrases", []),
                "last_seen": time.time(),
                "confidence": confidence,
                "frequency": rank,
                "size": size_val,
                "size_unit": size_unit,
            },
        )
        return True
