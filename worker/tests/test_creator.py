"""Creator normalization / matching tests (spec §2, §16)."""

import pytest

from gmv.creator import normalize_username, usernames_match


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@creator_name", "creator_name"),
        ("  creator_name  ", "creator_name"),
        ("https://www.tiktok.com/@creator_name", "creator_name"),
        ("https://www.tiktok.com/@creator_name/", "creator_name"),
        ("www.tiktok.com/@name?lang=en", "name"),
        ("@CreatorName", "creatorname"),
        ("wendy.sepulveda79", "wendy.sepulveda79"),
        ("ms.lovely.ivonne_s", "ms.lovely.ivonne_s"),
        ("@TikTok.com", "tiktok.com"),  # not a URL (no slash) -> kept verbatim, lowercased
    ],
)
def test_normalize(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "nan", "NaN"])
def test_normalize_empty(raw):
    assert normalize_username(raw) is None


def test_match_equivalent_forms():
    assert usernames_match("@CreatorName", "creatorname")
    assert usernames_match("creatorname", "https://www.tiktok.com/@creatorname")
    assert usernames_match("https://www.tiktok.com/@creatorname", "@CreatorName")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("creatorname", "creatorname1"),
        ("creatorname", "creator_name"),
        ("creator.name", "creatorname"),
    ],
)
def test_no_match_distinct(a, b):
    assert not usernames_match(a, b)


def test_no_match_when_either_empty():
    assert not usernames_match(None, "x")
    assert not usernames_match("x", None)
    assert not usernames_match(None, None)
