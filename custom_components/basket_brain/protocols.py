from __future__ import annotations

from typing import Any, Protocol


class GroceryClient(Protocol):
    """Common interface for all grocery chain HTTP clients."""

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search for products matching query."""
        ...

    async def get_price(self, product_id: str) -> dict[str, Any]:
        """Return pricing info for a single product."""
        ...

    async def check_authed(self) -> None:
        """Cheap probe: confirm the current cookie jar is still a live session.

        Returns None when signed in. Raises the chain's session-expired error
        (CookieExpiredError / FoodstuffsCookieExpiredError) when the jar is
        rejected. Does not mutate anything.
        """
        ...

    async def get_cart(self) -> dict[str, Any]:
        """Return current cart contents and totals."""
        ...

    async def add_to_cart(self, product_id: str, quantity: int) -> None:
        """Add a product to the cart."""
        ...

    async def list_usual(self) -> list[dict[str, Any]]:
        """Return the user's usual/frequently bought items."""
        ...

    async def search_by_barcode(self, gtin: str) -> dict[str, Any] | None:
        """Resolve a GTIN barcode to a product on this chain.

        Returns a normalised product dict or None if not found.
        """
        ...

    async def get_product_detail(self, product_id: str) -> dict[str, Any] | None:
        """Return full product detail including the `barcode` (GTIN) field.

        On Woolworths the detail endpoint also carries `breadcrumb`.
        On Foodstuffs the `barcode` key holds the GTIN from the `sku` field.
        Returns None on any error.
        """
        ...

    async def get_breadcrumb(self, product_id: str) -> dict[str, Any] | None:
        """Return the category breadcrumb/tree for a product, or None.

        Woolworths: returns the `breadcrumb` dict (department/aisle/shelf) from
        the product detail endpoint — not available on search results.
        Foodstuffs: returns None — `categoryTrees` is already inline on every
        search/list result; no separate call is needed.
        """
        ...

    async def get_specials(self) -> list[dict[str, Any]]:
        """Return products currently on special.

        Each item has: barcode (GTIN), product_id, name, now_price, was_price.
        Returns an empty list on any error so callers degrade gracefully.
        The coordinator intersects this with the product map to surface only
        items the user has bought before (usual-on-special).
        """
        ...
