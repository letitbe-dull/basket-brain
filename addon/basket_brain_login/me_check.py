"""Woolworths signed-in check: run the GraphQL `Me` query from a logged-in page."""

from __future__ import annotations

from typing import Any

# Runs in the page, so the session cookies ride along.
ME_SCRIPT = """() => fetch("/api/graphql?op-name=Me", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({operationName: "Me", query: "query Me { me { __typename id } }", variables: {}}),
}).then(r => r.json())"""


def is_customer(response: Any) -> bool:
    """True when a `Me` response says the session is a signed-in customer.

    @param response: parsed `Me` response body
    @returns True only for `me.__typename == "Customer"`
    """
    if not isinstance(response, dict):
        return False
    me = (response.get("data") or {}).get("me") or {}
    return me.get("__typename") == "Customer"


async def confirm_signed_in(page: Any) -> bool:
    """Run `Me` in the page and report whether it answers as a customer.

    @param page: Playwright-style page on woolworths.co.nz
    @returns True when signed in; False on a guest answer or any evaluation failure
    """
    try:
        return is_customer(await page.evaluate(ME_SCRIPT))
    except Exception:  # noqa: BLE001 - any failure means not confirmed
        return False
