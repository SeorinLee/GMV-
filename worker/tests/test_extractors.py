"""Network JSON extractor + login-URL detection tests (spec §11, §16, §9)."""

import pytest

from gmv.automation.extractors import extract_from_json
from gmv.automation.selectors import is_logged_out_url


def test_json_matches_username():
    payload = {
        "data": {
            "creators": [
                {"unique_id": "alpha", "gmv_str": "$5K", "items_sold": 10},
                {"unique_id": "beta", "gmv_str": "$9K", "items_sold": 3},
            ]
        }
    }
    assert extract_from_json(payload, "alpha") == ("$5K", 10)
    assert extract_from_json(payload, "beta") == ("$9K", 3)


def test_json_expands_abbreviated_and_range_items_sold():
    payload = {
        "creators": [
            {"unique_id": "alpha", "gmv_str": "$5K", "itemsSold": "4.7K"},
            {"unique_id": "beta", "gmv_str": "$9K", "itemsSold": "0-1.2K"},
        ]
    }
    assert extract_from_json(payload, "alpha", require_username=True) == ("$5K", 4700)
    assert extract_from_json(payload, "beta", require_username=True) == ("$9K", 1200)


def test_json_no_username_match_returns_none():
    payload = {"creators": [{"unique_id": "someone_else", "gmv": "$5K"}]}
    assert extract_from_json(payload, "alpha") == (None, None)


def test_json_single_result_without_username_field():
    payload = {"result": {"gmv": "$1.2M"}}
    assert extract_from_json(payload, "alpha") == ("$1.2M", None)


def test_json_strict_mode_rejects_username_less_payload():
    payload = {"result": {"gmv": "$1.2M"}}
    assert extract_from_json(payload, "alpha", require_username=True) == (None, None)


def test_json_no_gmv():
    assert extract_from_json({"foo": "bar"}, "alpha") == (None, None)


@pytest.mark.parametrize(
    ("url", "logged_out"),
    [
        ("https://affiliate-us.tiktok.com/platform/homepage?shop_region=US", False),
        ("https://affiliate-us.tiktok.com/login?redirect=x", True),
        ("https://www.tiktok.com/passport/web/", True),
        # /account/register is the normal Seller Center login entry, NOT a logged-out state.
        ("https://seller-us.tiktok.com/account/register", False),
        ("https://x.tiktok.com/?ttp_session_expire=1", True),
    ],
)
def test_is_logged_out_url(url, logged_out):
    assert is_logged_out_url(url) is logged_out
