import pytest
from bunkr_api.utils.formatting import slugify_filename, parse_selection, format_bytes

def test_slugify():
    assert slugify_filename(1, "Hello World!") == "01_Hello_World"
    assert slugify_filename(99, "Spaces   And Symbols!!!") == "99_Spaces_And_Symbols"

def test_format_bytes():
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1024 * 1024) == "1.00 MB"
    assert format_bytes(1024 * 1024 * 1024) == "1.00 GB"

def test_parse_selection_ranges():
    assert parse_selection("1-3, 5", total_items=5) == [1, 2, 3, 5]
    assert parse_selection("all", total_items=3) == [1, 2, 3]

def test_parse_selection_out_of_bounds():
    with pytest.raises(ValueError):
        parse_selection("10", total_items=5)

