"""Cookie grouping. Collapsing domains destroys the Foodstuffs shop session."""

from custom_components.basket_brain.login_client import _group_cookies_by_domain


def test_same_name_on_two_domains_stays_separate() -> None:
    grouped = _group_cookies_by_domain(
        [
            {
                "name": "refresh_token",
                "value": "CLUBPLUS",
                "domain": "login.clubplus.co.nz",
            },
            {"name": "refresh_token", "value": "SHOP", "domain": "www.paknsave.co.nz"},
        ]
    )

    assert grouped == {
        "login.clubplus.co.nz": {"refresh_token": "CLUBPLUS"},
        "www.paknsave.co.nz": {"refresh_token": "SHOP"},
    }


def test_leading_dot_stripped_from_domain() -> None:
    grouped = _group_cookies_by_domain(
        [{"name": "a", "value": "1", "domain": ".woolworths.co.nz"}]
    )

    assert grouped == {"woolworths.co.nz": {"a": "1"}}


def test_malformed_entries_dropped() -> None:
    grouped = _group_cookies_by_domain(
        [
            {"name": "a", "value": "1", "domain": "x.co.nz"},
            {"name": "b", "domain": "x.co.nz"},
            {"value": "2", "domain": "x.co.nz"},
            {"name": "c", "value": 3, "domain": "x.co.nz"},
            {},
        ]
    )

    assert grouped == {"x.co.nz": {"a": "1"}}
