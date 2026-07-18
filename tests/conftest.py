from typing import Any

import pytest
import pytest_socket

pytest_plugins = "pytest_homeassistant_custom_component"


# Loading the model2vec model pulls in huggingface_hub, which starts a tqdm
# progress bar. tqdm's monitor daemon ("TMonitor") outlives the test and trips
# HA's strict verify_cleanup thread check. Disabling the monitor interval stops
# that thread ever starting — no effect on the integration itself.
try:  # pragma: no cover - test-infra guard
    from tqdm import tqdm as _tqdm

    _tqdm.monitor_interval = 0
except Exception:  # tqdm may be absent when model2vec isn't installed
    pass


def _no_socket_block(*_args: Any, **_kwargs: Any) -> None:
    """No-op replacement for pytest_socket.disable_socket — see below."""


# pytest-homeassistant calls `disable_socket(allow_unix_socket=True)` from its
# own pytest_runtest_setup hook. On Linux, asyncio's self-pipe is a real
# socketpair, so it slips through that allowance. Windows has no native
# socketpair: it falls back to an AF_INET TCP pair, which gets blocked — so
# HA's ProactorEventLoop cannot even be constructed and every test errors in
# fixture setup. Neutering the call is the only reliable fix; hook ordering
# can't win, because pytest's own runner builds the fixtures inside that same
# hook. Nothing here touches the network: respx intercepts every request.
pytest_socket.disable_socket = _no_socket_block  # type: ignore[assignment]


class FakeClient:
    """Stand-in for a GroceryClient. Records calls, returns canned data."""

    def __init__(
        self,
        search_results: list[dict[str, Any]] | None = None,
        usual: list[dict[str, Any]] | None = None,
        search_error: Exception | None = None,
        add_error: Exception | None = None,
        barcode_results: dict[str, dict[str, Any] | None] | None = None,
        detail_results: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        self._search_results = search_results or []
        self._usual = usual or []
        self._search_error = search_error
        self._add_error = add_error
        # gtin → product dict (or None if not found on this chain)
        self._barcode_results: dict[str, dict[str, Any] | None] = barcode_results or {}
        # product_id → detail dict (or None on failure)
        self._detail_results: dict[str, dict[str, Any] | None] = detail_results or {}
        self.searched: list[str] = []
        self.added: list[tuple[str, int]] = []
        self.barcode_searched: list[str] = []
        self.detail_fetched: list[str] = []

    async def search(self, query: str) -> list[dict[str, Any]]:
        self.searched.append(query)
        if self._search_error:
            raise self._search_error
        return self._search_results

    async def get_price(self, product_id: str) -> dict[str, Any]:
        return {"id": product_id, "price": 1.0}

    async def get_prices(
        self, product_ids: list[str], concurrency: int = 5
    ) -> dict[str, dict[str, Any]]:
        return {pid: {"id": pid, "price": 1.0} for pid in product_ids}

    async def get_cart(self) -> dict[str, Any]:
        return {}

    async def add_to_cart(self, product_id: str, quantity: int) -> None:
        if self._add_error:
            raise self._add_error
        self.added.append((product_id, quantity))

    async def list_usual(self) -> list[dict[str, Any]]:
        return self._usual

    async def search_by_barcode(self, gtin: str) -> dict[str, Any] | None:
        self.barcode_searched.append(gtin)
        return self._barcode_results.get(gtin)

    async def get_product_detail(self, product_id: str) -> dict[str, Any] | None:
        self.detail_fetched.append(product_id)
        return self._detail_results.get(product_id)

    async def get_breadcrumb(self, product_id: str) -> dict[str, Any] | None:
        return None


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
