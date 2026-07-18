"""Phrase → product resolution. Map-first with a category-gated fallback."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .foodstuffs import FoodstuffsCookieExpiredError
from .product_map import confident_map_match
from .product_utils import (
    _product_id,
    extract_brand,
    name_satisfies,
    parse_size,
    product_size,
    variant_markers,
)
from .protocols import GroceryClient
from .woolworths import CookieExpiredError

if TYPE_CHECKING:
    from .product_map import ProductMap

_LOGGER = logging.getLogger(__name__)

_ALIASES_FILENAME = "basket_brain_aliases.json"


def _item_name(item: dict[str, Any]) -> str:
    return item.get("name") or item.get("title") or item.get("productName") or ""


def _item_price(item: dict[str, Any]) -> float | None:
    """Return a comparable price from a search hit, or None if absent.

    Foodstuffs normalised hits carry `price_nzd`; Woolworths hits carry a
    flattened `price` (see `_parse_price`). Anything unparseable is treated
    as "no price" so cheapest-picks fall back to first-in-list.
    """
    for key in ("price_nzd", "price"):
        val = item.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _sized_like(hit: dict[str, Any], unit: str) -> float | None:
    """Return the hit's normalised size *in the anchor's unit*, or None.

    Mixed units (kg vs each) can't be compared meaningfully, so we drop them
    rather than pretending 1kg ≈ 1 pack.
    """
    result = product_size(hit)
    if result is None or result[1] != unit:
        return None
    return result[0]


def _cheapest(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the cheapest hit; ties and unpriced hits keep list order."""
    priced = [(h, _item_price(h)) for h in hits]
    with_price = [(h, p) for h, p in priced if p is not None]
    if with_price:
        return min(with_price, key=lambda x: x[1])[0]
    return hits[0]


def _anchor_empty(anchor: dict[str, Any] | None) -> bool:
    """True when the anchor carries no useful brand or size for cross-chain matching."""
    if anchor is None:
        return True
    return anchor.get("brand") is None and anchor.get("size") is None


def _resolution(
    product_id: str, confidence: str, reason: str
) -> dict[str, Any]:
    """The shape every resolve path returns. Kept as a plain dict so it can
    ride along in coordinator.data and JSON-serialise without ceremony.
    """
    return {"product_id": product_id, "confidence": confidence, "reason": reason}


def _category_matches_hit(
    hit: dict[str, Any], chain: str, expected_l1: str
) -> bool | None:
    """Cheap inline category check for a search hit.

    Returns True/False when the hit already carries the info (Foodstuffs
    ships `categoryTrees` inline); returns None when the chain needs a
    per-hit detail fetch (Woolworths breadcrumb) — the caller decides
    whether to spend that call.
    """
    if chain == "woolworths":
        return None
    trees = hit.get("categoryTrees") or []
    return any(
        (t.get("level0") or "").strip().lower() == expected_l1 for t in trees
    )


class ShoppingListManager:
    """Resolve shopping-list phrases to per-chain product IDs.

    Order:
      1. Pinned alias (user override).
      2. Product map — phrase learned before, or a fuzzy token match.
      3. Category-gated live search on the chain.
      4. Fallback: search's top hit, tagged low-confidence for approval.

    Every hit returns a dict {product_id, confidence, reason}; None means
    the phrase couldn't be resolved on this chain at all.
    """

    def __init__(self, product_map: ProductMap | None = None) -> None:
        # {chain: {phrase_lower: product_id}}
        self._aliases: dict[str, dict[str, str]] = {}
        self._map = product_map

    def set_map(self, product_map: ProductMap) -> None:
        """Late-bind the map (coordinator loads it after resolver is built)."""
        self._map = product_map

    @staticmethod
    async def get_list_items(hass: HomeAssistant) -> list[str]:
        """Return pending item phrases from todo.shopping_list.

        Todo entities only expose an item *count* as state — the items
        themselves must be fetched via the todo.get_items service.
        """
        if hass.states.get("todo.shopping_list") is None:
            return []
        result = await hass.services.async_call(
            "todo",
            "get_items",
            {"entity_id": "todo.shopping_list", "status": "needs_action"},
            blocking=True,
            return_response=True,
        )
        items: list[dict[str, Any]] = (
            (result or {}).get("todo.shopping_list", {}).get("items", [])
        )
        return [item["summary"] for item in items if item.get("summary")]

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def resolve(
        self,
        phrase: str,
        chain: str,
        hass: HomeAssistant | None,
        exclude: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Resolve without live search (alias, then map).

        `exclude` is the set of product IDs to skip on this chain — used to
        pick the next-best when a first choice turned out to be out of stock.
        Live search is handled by `_resolve_with_client`.
        """
        exclude = exclude or set()
        phrase_lower = phrase.lower()

        # Tier 1: pinned alias — an explicit user override, always wins.
        alias = self._aliases.get(chain, {}).get(phrase_lower)
        if alias is not None and alias not in exclude:
            return _resolution(alias, "high", "alias")

        # Tier 2: product map.
        return self._map_lookup(phrase_lower, chain, exclude)

    def _map_lookup(
        self, phrase_lower: str, chain: str, exclude: set[str] | None = None
    ) -> dict[str, Any] | None:
        """Try the map — first by learned phrase, then by fuzzy token score."""
        if self._map is None:
            return None
        exclude = exclude or set()

        # 2a: learned phrase — O(1) via the map's phrase index.
        by_phrase = self._map.get_by_phrase(phrase_lower)
        if by_phrase:
            _, entry = by_phrase
            pid = (entry.get("chains") or {}).get(chain)
            if pid and pid not in exclude:
                return _resolution(pid, "high", "map_phrase")

        # 2b: fuzzy token match over mapped product names. Only commit — and
        # only learn the phrase — when the top match is strong and unambiguous.
        # A weak or cross-category match (e.g. "cheese" hitting a corn chip) is
        # left for live search rather than cemented onto a look-alike, which is
        # exactly what once learned cheese → Doritos.
        matches = self._map.match_phrase(phrase_lower)
        if not confident_map_match(matches):
            return None
        for gtin, entry, _ in matches:
            pid = (entry.get("chains") or {}).get(chain)
            if pid and pid not in exclude:
                self._map.add_phrase(gtin, phrase_lower)
                return _resolution(pid, "high", "map_match")

        return None

    async def _resolve_with_client(
        self,
        phrase: str,
        chain: str,
        client: GroceryClient,
        hass: HomeAssistant | None,
        exclude: set[str] | None = None,
        anchor: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Full resolution: alias → map → category-gated search → top-hit fallback.

        `anchor` is an explicit brand+size reference (the primary chain's
        resolved product) used to pick a like-for-like match when the map has
        no entry — the cross-chain fallback when a barcode differs between
        chains for the same product.
        """
        exclude = exclude or set()
        result = await self.resolve(phrase, chain, hass, exclude)
        if result is not None:
            return result

        # Tier 3: live search.
        try:
            hits = await client.search(phrase)
        except (CookieExpiredError, FoodstuffsCookieExpiredError):
            # An expired session is not a per-item failure — let it out so
            # the coordinator can re-login. Swallowing it here is what left
            # a chain silently resolving nothing until the next scheduled
            # relogin (see the same lesson already learned for Woolworths'
            # price fetch in coordinator.py).
            raise
        except Exception:
            _LOGGER.exception("Search failed for phrase %r on chain %r", phrase, chain)
            return None

        # Drop any candidate we've been told to skip (out of stock last time).
        if exclude:
            hits = [h for h in hits if _product_id(h) not in exclude]
        if not hits:
            return None

        return await self._pick_from_search(phrase, chain, hits, client, anchor)

    def _anchor_for_phrase(self, phrase_lower: str) -> dict[str, Any] | None:
        """Return the best-matching map entry for a phrase, or None.

        The anchor is the top-scoring map entry for the phrase — it defines the
        brand + size we want the search fallback to match on the other chain.
        """
        if not self._map:
            return None
        matches = self._map.match_phrase(phrase_lower)
        return matches[0][1] if matches else None

    async def _pick_from_search(
        self,
        phrase: str,
        chain: str,
        hits: list[dict[str, Any]],
        client: GroceryClient,
        anchor_override: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Choose the best hit from a live search.

        Category-gates the token matches first. When we have an anchor (brand +
        normalised size) — either the primary chain's resolved product passed
        in as `anchor_override`, or the best map entry for the phrase — a
        brand-first size-aware tree picks the like-for-like match, so a 50g
        Cadbury bar beats a sharepack and a 1L milk never scores against a 2L.
        Without an anchor, or with size unknown, this falls back to the first
        gated token match.
        """
        phrase_lower = phrase.lower()
        if anchor_override is not None:
            # Cross-chain: the primary product is the authority. Don't let a
            # fuzzy map lookup (e.g. "crunchie" ~ "crunch") impose a bogus
            # category gate that throws out the genuine candidates.
            anchor = anchor_override
            expected_cat = None
        else:
            # The anchor (brand + size for the tree) requires a confident,
            # unambiguous map match. The category gate is looser — we apply it
            # whenever the map has *any* signal, even an ambiguous one, so that
            # "cheese" (hitting Pantry/Frozen/Bakery) still rejects a Fridge
            # result rather than letting a fridge cheese through at high
            # confidence just because the map couldn't pick one department.
            map_matches = self._map.match_phrase(phrase_lower) if self._map else []
            use_map = confident_map_match(map_matches)
            anchor = map_matches[0][1] if use_map else None
            expected_cat = (
                self._map.category_for_phrase(phrase_lower) if map_matches else None
            )

        matched = [
            h for h in hits if name_satisfies(phrase_lower, _item_name(h)) is not None
        ]

        # Category-gate the token matches (async — inline, once).
        if matched and expected_cat:
            gated: list[dict[str, Any]] = []
            for hit in matched:
                if await self._category_ok(hit, chain, expected_cat, client):
                    gated.append(hit)
            candidates = gated
            gated_reason = "search_gated"
        else:
            candidates = matched
            gated_reason = "search_match"

        # If nothing genuinely IS the phrase, top-hit low as before.
        if not candidates:
            pid = _product_id(hits[0])
            if pid:
                _LOGGER.debug(
                    "Resolved %r on %s via low-confidence top hit: %s",
                    phrase, chain, _item_name(hits[0]),
                )
                return _resolution(pid, "low", "search_top_hit")
            return None

        # No anchor, or anchor has no known size → skip the tree, keep the
        # pre-Phase-3 behaviour (first surviving head-noun match wins).
        anchor_size = anchor.get("size") if anchor else None
        anchor_unit = anchor.get("size_unit") if anchor else None
        if anchor is None or anchor_size is None or not anchor_unit:
            for hit in candidates:
                pid = _product_id(hit)
                if pid:
                    return _resolution(pid, "high", gated_reason)
            return None

        return self._brand_size_tree(
            candidates, anchor, anchor_size, anchor_unit
        )

    @staticmethod
    def _brand_size_tree(
        candidates: list[dict[str, Any]],
        anchor: dict[str, Any],
        anchor_size: float,
        anchor_unit: str,
    ) -> dict[str, Any] | None:
        """Brand-first, size-aware pick among category-gated candidates.

        0. Drop candidates whose variant (UHT, powdered, concentrate) differs
           from the anchor's — unless that leaves nothing.
        1. Brand match + exact size → cheapest of those (high,
           search_brand_size).
        2. Brand match, no exact size → nearest size of that brand (high,
           search_brand_nearest).
        3. No brand match → cheapest at exact same size, any brand (high,
           search_size_cheapest).
        4. No exact size → cheapest within ±25% band, any brand (low,
           search_similar_size).
        5. Nothing in band → flag (low, search_needs_ok).

        "Exact size" allows ±1% for rounding; the ±25% band only applies to
        step 4. Prices come from `_item_price`; ties keep list order.
        """
        anchor_brand = anchor.get("brand")
        anchor_brand_norm = (
            extract_brand({"brand": anchor_brand}) if anchor_brand else None
        )

        # Variant gate: keep only candidates that are the same *kind* of thing
        # as the anchor — same size and a lower price is exactly how a 1L UHT
        # used to win against 1L fresh milk. Skipped when it would leave
        # nothing, so a chain that only stocks the other variant still resolves.
        anchor_variants = variant_markers(anchor.get("name") or "")
        same_variant = [
            h for h in candidates
            if variant_markers(_item_name(h)) == anchor_variants
        ]
        if same_variant:
            candidates = same_variant

        def size_of(hit: dict[str, Any]) -> float | None:
            return _sized_like(hit, anchor_unit)

        def within(hit_size: float, tolerance: float) -> bool:
            return abs(hit_size - anchor_size) <= anchor_size * tolerance

        brand_matches = (
            [h for h in candidates if extract_brand(h) == anchor_brand_norm]
            if anchor_brand_norm
            else []
        )

        # Step 1 & 2: brand match branch.
        if brand_matches:
            exact = [
                h for h in brand_matches
                if (s := size_of(h)) is not None and within(s, 0.01)
            ]
            if exact:
                pid = _product_id(_cheapest(exact))
                if pid:
                    return _resolution(pid, "high", "search_brand_size")

            sized = [
                (h, s) for h in brand_matches
                if (s := size_of(h)) is not None
            ]
            if sized:
                nearest = min(sized, key=lambda x: abs(x[1] - anchor_size))
                pid = _product_id(nearest[0])
                if pid:
                    return _resolution(pid, "high", "search_brand_nearest")
            # Brand matched but no candidate carried a comparable size —
            # fall through to the any-brand branches.

        # Step 3: cheapest at exact size, any brand.
        exact_any = [
            h for h in candidates
            if (s := size_of(h)) is not None and within(s, 0.01)
        ]
        if exact_any:
            pid = _product_id(_cheapest(exact_any))
            if pid:
                return _resolution(pid, "high", "search_size_cheapest")

        # Step 4: cheapest within ±25%.
        in_band = [
            h for h in candidates
            if (s := size_of(h)) is not None and within(s, 0.25)
        ]
        if in_band:
            pid = _product_id(_cheapest(in_band))
            if pid:
                return _resolution(pid, "low", "search_similar_size")

        # Step 5: nothing in band — flag.
        pid = _product_id(candidates[0])
        if pid:
            return _resolution(pid, "low", "search_needs_ok")
        return None

    @staticmethod
    async def _category_ok(
        hit: dict[str, Any],
        chain: str,
        expected_l1: str,
        client: GroceryClient,
    ) -> bool:
        """True when this hit's L1 category matches the expected one.

        Foodstuffs hits carry `categoryTrees` inline — cheap. Woolworths hits
        don't, so we spend one breadcrumb call per candidate; the caller has
        already narrowed to head-noun matches so this is 1–3 calls at most.
        """
        inline = _category_matches_hit(hit, chain, expected_l1)
        if inline is not None:
            return inline
        product_id = _product_id(hit)
        if not product_id:
            return False
        try:
            bc = await client.get_breadcrumb(product_id)
        except (CookieExpiredError, FoodstuffsCookieExpiredError):
            raise
        except Exception:
            return False
        if not bc:
            return False
        dept = (bc.get("department") or {}).get("name", "").strip().lower()
        return dept == expected_l1

    async def resolve_all(
        self,
        hass: HomeAssistant | None,
        clients: dict[str, GroceryClient] | None = None,
        primary_chain: str | None = None,
        exclude: dict[str, dict[str, set[str]]] | None = None,
    ) -> dict[str, dict[str, dict[str, Any] | None]]:
        """Resolve every pending list item for every enabled chain.

        For a phrase with no map entry on some chains, resolving each chain
        independently lets a live text search pick a *different physical
        product* per chain — the same phrase can end up as different items
        on different chains. Instead: resolve the primary chain first (alias
        → map → live search, whichever tier wins), read that pick's barcode,
        then resolve every other still-unresolved chain by that barcode —
        the same GTIN, not a fresh guess. Only a chain with no barcode hit
        falls back to its own independent live search.

        Returns `{phrase: {chain: resolution | None}}`, where resolution is
        `{product_id, confidence, reason}`.
        """
        phrases = await self.get_list_items(hass)
        if not phrases:
            return {}

        clients = clients or {}
        primary = primary_chain if primary_chain in clients else None
        exclude = exclude or {}
        result: dict[str, dict[str, dict[str, Any] | None]] = {}

        for phrase in phrases:
            skip = exclude.get(phrase, {})

            # Tier 1/2 first, for every chain — cheap, no network.
            per_chain: dict[str, dict[str, Any] | None] = {
                chain: await self.resolve(phrase, chain, hass, skip.get(chain))
                for chain in clients
            }

            anchor_gtin = None
            primary_anchor = None
            if primary is not None:
                primary_client = clients[primary]
                if per_chain[primary] is None:
                    per_chain[primary] = await self._resolve_with_client(
                        phrase, primary, primary_client, hass, skip.get(primary)
                    )
                primary_res = per_chain[primary]
                if primary_res and primary_res.get("product_id"):
                    detail = await self._primary_detail(
                        primary_client, primary_res["product_id"]
                    )
                    if detail:
                        anchor_gtin = detail.get("barcode")
                        primary_anchor = self._anchor_from_detail(detail)

            for chain, client in clients.items():
                if chain == primary or per_chain[chain] is not None:
                    continue
                chain_skip = skip.get(chain)
                resolved = None
                barcode_miss = False
                if anchor_gtin:
                    resolved = await self._resolve_by_barcode(
                        anchor_gtin, chain, client, chain_skip
                    )
                    if resolved is None:
                        barcode_miss = True
                if resolved is None:
                    # Barcode missed (or none) — the same product can carry a
                    # different GTIN per chain, so match on the primary's
                    # brand + size instead of guessing at a top hit.
                    resolved = await self._resolve_with_client(
                        phrase, chain, client, hass, chain_skip, primary_anchor
                    )
                    if resolved is not None and barcode_miss and _anchor_empty(
                        primary_anchor
                    ):
                        # No anchor (or an empty one) — the search result is a
                        # blind guess, so cap to low confidence for approval.
                        resolved = {**resolved, "confidence": "low"}
                per_chain[chain] = resolved

            result[phrase] = per_chain

        return result

    @staticmethod
    async def _primary_detail(
        client: GroceryClient, product_id: str
    ) -> dict[str, Any] | None:
        """Fetch the primary chain's product detail, or None on any failure."""
        try:
            return await client.get_product_detail(product_id)
        except (CookieExpiredError, FoodstuffsCookieExpiredError):
            raise
        except Exception:
            _LOGGER.exception(
                "get_product_detail failed for product_id %r", product_id
            )
            return None

    @staticmethod
    def _anchor_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
        """Build a brand+size anchor from a primary-chain product detail.

        Woolworths nests the pack size under ``size.volumeSize`` (e.g. "50g"),
        which the generic `product_size` doesn't read, so try that first and
        fall back to the generic extractor.
        """
        size_res = None
        sz = detail.get("size")
        if isinstance(sz, dict) and sz.get("volumeSize"):
            size_res = parse_size(str(sz["volumeSize"]))
        if size_res is None:
            size_res = product_size(detail)
        return {
            "brand": detail.get("brand"),
            "name": _item_name(detail),
            "size": size_res[0] if size_res else None,
            "size_unit": size_res[1] if size_res else None,
        }

    @staticmethod
    async def _resolve_by_barcode(
        gtin: str, chain: str, client: GroceryClient, exclude: set[str] | None = None
    ) -> dict[str, Any] | None:
        """Resolve a chain to the exact product carrying this GTIN, or None."""
        try:
            hit = await client.search_by_barcode(gtin)
        except (CookieExpiredError, FoodstuffsCookieExpiredError):
            raise
        except Exception:
            _LOGGER.exception(
                "search_by_barcode failed for gtin %r on chain %r", gtin, chain
            )
            return None
        if not hit:
            return None
        pid = _product_id(hit)
        if not pid or (exclude and pid in exclude):
            return None
        return _resolution(pid, "high", "barcode_cross_chain")

    # ------------------------------------------------------------------
    # Aliases (pinned overrides)
    # ------------------------------------------------------------------

    def pin_alias(self, phrase: str, chain: str, product_id: str) -> None:
        self._aliases.setdefault(chain, {})[phrase.lower()] = product_id

    async def async_save_aliases(self, hass: HomeAssistant) -> None:
        path = hass.config.path(_ALIASES_FILENAME)
        await hass.async_add_executor_job(self._save_aliases, path)

    def _save_aliases(self, path: str) -> None:
        """Blocking write — must run in the executor."""
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._aliases, fh, indent=2)
        except OSError:
            _LOGGER.exception("Could not save aliases to %s", path)

    async def async_load_aliases(self, hass: HomeAssistant) -> None:
        """Load aliases from disk; silently starts fresh if missing or corrupt."""
        path = hass.config.path(_ALIASES_FILENAME)
        await hass.async_add_executor_job(self._load_aliases, path)

    def _load_aliases(self, path: str) -> None:
        """Blocking read — must run in the executor."""
        if not Path(path).is_file():
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._aliases = data
        except (OSError, json.JSONDecodeError):
            _LOGGER.warning(
                "Could not read aliases from %s — starting with empty aliases", path
            )
