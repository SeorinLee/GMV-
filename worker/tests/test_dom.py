"""Pure DOM-text extraction tests (spec §11)."""

from gmv.automation.dom import extract_from_row_text, result_signature


def test_extract_exact_gmv_and_items():
    text = "alpha\nCreator\n$1,234.56\n789\nfollowers"
    gmv, items = extract_from_row_text(text)
    assert gmv == "$1,234.56"
    assert items == 789


def test_extract_range_gmv_kept_verbatim():
    gmv, items = extract_from_row_text("beta\n$1K - $5K\n42")
    assert gmv == "$1K - $5K"  # parser handles the range downstream
    assert items == 42


def test_extract_missing_gmv():
    gmv, items = extract_from_row_text("gamma\nno money here\n123abc")
    assert gmv is None
    assert items is None


def test_items_optional():
    gmv, items = extract_from_row_text("delta\n$5K")
    assert gmv == "$5K"
    assert items is None


def test_signature_changes_with_content():
    assert result_signature("a\n$5K\n") != result_signature("b\n$9K\n")
    assert result_signature("  a \n $5K ") == result_signature("a\n$5K")
