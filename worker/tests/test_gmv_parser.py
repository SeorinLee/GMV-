"""GMV parser unit tests — covers every input listed in spec §5."""

from decimal import Decimal

import pytest

from gmv.gmv_parser import parse_gmv
from gmv.models import GmvValueType


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$0", Decimal(0)),
        ("$1", Decimal(1)),
        ("$1,234", Decimal(1234)),
        ("$1,234.56", Decimal("1234.56")),
        ("$853.4K", Decimal(853400)),
        ("853.4K", Decimal(853400)),
        ("$1.2M", Decimal(1_200_000)),
        ("$2M", Decimal(2_000_000)),
        ("$0.5K", Decimal(500)),
        ("$4,253.21", Decimal("4253.21")),
        ("$2B", Decimal(2_000_000_000)),
        ("£4.2K", Decimal(4200)),
    ],
)
def test_exact_values(raw, expected):
    result = parse_gmv(raw)
    assert result.value_type is GmvValueType.EXACT
    assert result.value == expected
    assert result.raw_value == raw


@pytest.mark.parametrize(
    ("raw", "expected_max", "expected_min"),
    [
        ("$0-$5K", Decimal(5000), Decimal(0)),
        ("$1K-$5K", Decimal(5000), Decimal(1000)),
        ("$5K-$10K", Decimal(10000), Decimal(5000)),
        ("$1.2M-$1.5M", Decimal(1_500_000), Decimal(1_200_000)),
        ("$0 - $5,000", Decimal(5000), Decimal(0)),
        ("0 to 5K", Decimal(5000), Decimal(0)),
    ],
)
def test_range_takes_max(raw, expected_max, expected_min):
    result = parse_gmv(raw)
    assert result.value_type is GmvValueType.RANGE_MAX
    assert result.value == expected_max
    assert result.upper_bound == expected_max
    assert result.lower_bound == expected_min


@pytest.mark.parametrize("raw", ["Under $5K", "Below $5K", "<$5K", "Up to $5K", "Less than $5K"])
def test_capped_is_range_max(raw):
    result = parse_gmv(raw)
    assert result.value_type is GmvValueType.RANGE_MAX
    assert result.value == Decimal(5000)
    assert result.lower_bound == Decimal(0)
    assert result.upper_bound == Decimal(5000)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$5K+", Decimal(5000)),
        ("Over $5K", Decimal(5000)),
        ("Above $5K", Decimal(5000)),
        ("More than $5K", Decimal(5000)),
        ("$1M+", Decimal(1_000_000)),
    ],
)
def test_open_ended_is_estimate(raw, expected):
    result = parse_gmv(raw)
    assert result.value_type is GmvValueType.OPEN_ENDED_ESTIMATE
    assert result.value == expected
    assert result.lower_bound == expected
    assert result.upper_bound is None


@pytest.mark.parametrize("raw", ["", "   ", "N/A", "n/a", "-", "None", None])
def test_not_found(raw):
    result = parse_gmv(raw)
    assert result.value_type is GmvValueType.NOT_FOUND
    assert result.value is None


@pytest.mark.parametrize("raw", ["잘못된 문자열", "abc", "no data here", "$$$"])
def test_invalid_is_error(raw):
    result = parse_gmv(raw)
    assert result.value_type is GmvValueType.ERROR
    assert result.value is None


def test_open_ended_never_reports_exact():
    """Regression for D3: $5K+ must not be EXACT."""
    assert parse_gmv("$5K+").value_type is not GmvValueType.EXACT


def test_value_is_decimal_not_float():
    assert isinstance(parse_gmv("$853.4K").value, Decimal)
