"""Add-on Woolworths login: the `Me` signed-in confirmation and its use in the login flow."""

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ADDON = Path(__file__).parent.parent / "addon" / "basket_brain_login"
FIXTURES = Path(__file__).parent / "fixtures" / "woolworths_graphql"
sys.path.insert(0, str(ADDON))

from me_check import confirm_signed_in, is_customer  # noqa: E402


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakePage:
    """Page stand-in: every browser call is a no-op, evaluate returns a canned `Me` answer."""

    def __init__(self, me: Any) -> None:
        self._me = me
        self.url = "https://www.woolworths.co.nz/"

    async def evaluate(self, _script: str) -> Any:
        if isinstance(self._me, Exception):
            raise self._me
        return self._me

    def __getattr__(self, _name: str) -> Any:
        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _noop


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_fixture("me-signed-in.json"), True),
        (_fixture("me-no-cookies.json"), False),
        ({"errors": [{"message": "no", "extensions": {"code": "BANNED_OPERATION"}}]}, False),
        ({"data": None}, False),
        ({"data": {"me": {"__typename": "Guest"}}}, False),
        (None, False),
        ("oops", False),
    ],
)
def test_is_customer(response: Any, expected: bool) -> None:
    assert is_customer(response) is expected


async def test_confirm_signed_in_true_for_customer() -> None:
    assert await confirm_signed_in(FakePage(_fixture("me-signed-in.json"))) is True


async def test_confirm_signed_in_false_when_evaluate_raises() -> None:
    assert await confirm_signed_in(FakePage(RuntimeError("page gone"))) is False


@pytest.fixture
def login_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Import login_woolworths with camoufox stubbed out."""
    camoufox = types.ModuleType("camoufox")
    async_api = types.ModuleType("camoufox.async_api")
    async_api.AsyncCamoufox = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "camoufox", camoufox)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", async_api)
    monkeypatch.delitem(sys.modules, "login_woolworths", raising=False)
    return importlib.import_module("login_woolworths")


async def test_login_that_ends_as_guest_is_transient(login_module: types.ModuleType) -> None:
    with pytest.raises(login_module.TransientLoginError):
        await login_module._perform_login(FakePage(_fixture("me-no-cookies.json")), "a@b.c", "pw")


async def test_login_that_ends_as_customer_succeeds(login_module: types.ModuleType) -> None:
    await login_module._perform_login(FakePage(_fixture("me-signed-in.json")), "a@b.c", "pw")


def test_no_success_decision_reads_xsrf() -> None:
    assert "XSRF" not in (ADDON / "login_woolworths.py").read_text(encoding="utf-8")
