"""Browser/market profile configuration tests."""

import pytest

from gmv.config import (
    UK_CHROME_PROFILE_CODE,
    UK_EDGE_PROFILE_CODE,
    UK_PROFILE_CODE,
    US_CHROME_PROFILE_CODE,
    US_EDGE_PROFILE_CODE,
    US_PROFILE_CODE,
    get_default_profile,
    get_profile,
    list_profiles,
    resolve_profile_code,
)


def test_default_profile_is_us_chrome_and_reuses_legacy_storage():
    profile = get_default_profile()
    assert profile.profile_code == US_PROFILE_CODE
    assert profile.browser_channel == "chrome"
    assert profile.display_name == "Chrome · United States"
    assert profile.shop_region == "US"
    assert profile.storage_key == "DEFAULT"


def test_profiles_include_every_browser_market_combination():
    assert {p.profile_code for p in list_profiles()} == {
        US_CHROME_PROFILE_CODE,
        UK_CHROME_PROFILE_CODE,
        US_EDGE_PROFILE_CODE,
        UK_EDGE_PROFILE_CODE,
    }


def test_get_profile_supports_legacy_default_alias():
    assert get_profile("DEFAULT").profile_code == US_PROFILE_CODE
    assert get_profile("chrome").profile_code == US_PROFILE_CODE
    assert get_profile("edge").profile_code == UK_PROFILE_CODE
    assert get_profile("UK_CHROME").profile_code == UK_CHROME_PROFILE_CODE
    assert get_profile("US_EDGE").profile_code == US_EDGE_PROFILE_CODE


def test_get_profile_rejects_unknown_value():
    with pytest.raises(KeyError):
        get_profile("anything")


def test_us_browser_channel_env_override(monkeypatch):
    monkeypatch.setenv("GMV_BROWSER_CHANNEL", "custom-chrome")
    assert get_profile(US_PROFILE_CODE).browser_channel == "custom-chrome"


def test_shared_us_affiliate_env_override_is_normalized_to_regional_host(monkeypatch):
    monkeypatch.setenv(
        "GMV_FIND_CREATORS_URL", "https://affiliate.tiktok.com/connection/creator"
    )
    assert get_profile(US_PROFILE_CODE).find_creators_url == (
        "https://affiliate-us.tiktok.com/connection/creator"
    )


def test_uk_browser_channel_defaults_to_edge(monkeypatch):
    monkeypatch.delenv("GMV_UK_BROWSER_CHANNEL", raising=False)
    assert get_profile(UK_PROFILE_CODE).browser_channel == "msedge"


def test_uk_browser_channel_env_override(monkeypatch):
    monkeypatch.setenv("GMV_UK_BROWSER_CHANNEL", "custom-edge")
    assert get_profile(UK_PROFILE_CODE).browser_channel == "custom-edge"


def test_us_display_name_env_override(monkeypatch):
    monkeypatch.setenv("GMV_US_PROFILE_NAME", "내 미국 계정")
    assert get_profile(US_PROFILE_CODE).display_name == "내 미국 계정"


def test_us_routes_are_separate_and_direct_to_creator(monkeypatch):
    for name in (
        "GMV_LOGIN_URL",
        "GMV_LOGIN_SUCCESS_URL",
        "GMV_AFFILIATE_ENTRY_URL",
        "GMV_FIND_CREATORS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = get_profile(US_PROFILE_CODE)
    assert profile.login_url == "https://seller-us.tiktok.com/account/register"
    assert profile.login_success_url == (
        "https://seller-us.tiktok.com/affiliate/landing?shop_region=US"
    )
    assert profile.affiliate_entry_url == (
        "https://affiliate-us.tiktok.com/affiliate/collaboration/target-invitation"
        "?shop_region=US&route_migration=1&tab=1"
    )
    assert profile.find_creators_url == (
        "https://affiliate-us.tiktok.com/connection/creator?shop_region=US"
    )


def test_uk_profile_uses_edge_and_uk_hosts(monkeypatch):
    for name in (
        "GMV_UK_LOGIN_URL",
        "GMV_UK_LOGIN_SUCCESS_URL",
        "GMV_UK_AFFILIATE_ENTRY_URL",
        "GMV_UK_FIND_CREATORS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = get_profile(UK_PROFILE_CODE)
    assert profile.browser_channel == "msedge"
    assert profile.market == "UK"
    assert profile.shop_region == "GB"
    assert profile.login_url == "https://seller-uk.tiktok.com/"
    assert profile.login_success_url.startswith("https://seller-uk.tiktok.com/")
    assert profile.affiliate_entry_url == (
        "https://affiliate.tiktok.com/connection/target-invitation?shop_region=GB"
    )
    assert profile.find_creators_url == (
        "https://affiliate.tiktok.com/connection/creator?shop_region=GB"
    )


def test_browser_and_market_choices_are_independent(monkeypatch):
    for name in (
        "GMV_BROWSER_CHANNEL",
        "GMV_FIND_CREATORS_URL",
        "GMV_UK_BROWSER_CHANNEL",
        "GMV_UK_FIND_CREATORS_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    profiles = {profile.profile_code: profile for profile in list_profiles()}
    for code in (US_CHROME_PROFILE_CODE, UK_CHROME_PROFILE_CODE):
        assert profiles[code].browser_channel == "chrome"
    for code in (US_EDGE_PROFILE_CODE, UK_EDGE_PROFILE_CODE):
        assert profiles[code].browser_channel == "msedge"
    for code in (US_CHROME_PROFILE_CODE, US_EDGE_PROFILE_CODE):
        assert profiles[code].shop_region == "US"
        assert profiles[code].find_creators_url.startswith(
            "https://affiliate-us.tiktok.com/"
        )
        assert "shop_region=US" in profiles[code].find_creators_url
    for code in (UK_CHROME_PROFILE_CODE, UK_EDGE_PROFILE_CODE):
        assert profiles[code].shop_region == "GB"
        assert profiles[code].find_creators_url.startswith("https://affiliate.tiktok.com/")
        assert "shop_region=GB" in profiles[code].find_creators_url


def test_every_browser_market_combination_has_separate_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("GMV_PROFILE_ROOT", str(tmp_path))
    profiles = list_profiles()
    assert len({profile.storage_root for profile in profiles}) == 4
    assert get_profile(US_PROFILE_CODE).storage_root.endswith("DEFAULT")
    assert get_profile(UK_PROFILE_CODE).storage_root.endswith(UK_PROFILE_CODE)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("US", US_PROFILE_CODE),
        ("USA", US_PROFILE_CODE),
        ("United States", US_PROFILE_CODE),
        ("미국", US_PROFILE_CODE),
        ("Chrome", US_PROFILE_CODE),
        ("DEFAULT", US_PROFILE_CODE),
        ("UK", UK_PROFILE_CODE),
        ("GB", UK_PROFILE_CODE),
        ("United Kingdom", UK_PROFILE_CODE),
        ("영국", UK_PROFILE_CODE),
        ("Edge", UK_PROFILE_CODE),
        ("UK_CHROME", UK_CHROME_PROFILE_CODE),
        ("US_EDGE", US_EDGE_PROFILE_CODE),
    ],
)
def test_resolve_profile_code(value, expected):
    assert resolve_profile_code(value) == expected


@pytest.mark.parametrize("value", ["", None, "France", "xyz"])
def test_resolve_unknown_returns_none(value):
    assert resolve_profile_code(value) is None
