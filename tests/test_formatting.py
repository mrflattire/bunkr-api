from unittest.mock import patch

import pytest

from bunkr_api.utils.formatting import (
    clean_dragged_path,
    extract_expiry_from_url,
    format_bytes,
    get_album_folder_name,
    parse_and_check_expiry,
    parse_selection,
    sanitize_filename_simple,
    slugify_filename,
)

# ============================================================
# format_bytes
# ============================================================

@pytest.mark.parametrize(
    "num_bytes, expected",
    [
        (0, "0.00 B"),
        (500, "500.00 B"),
        (1024, "1.00 KB"),
        (1024 * 1024, "1.00 MB"),
        (1024 * 1024 * 1024, "1.00 GB"),
        (1024 ** 4, "1.00 TB"),
        (1024 ** 5, "1.00 PB"),
    ],
)
def test_format_bytes_units(num_bytes, expected):
    assert format_bytes(num_bytes) == expected


def test_format_bytes_non_numeric_passthrough():
    """Non-numeric input is stringified as-is rather than raising."""
    assert format_bytes("unknown") == "unknown"
    assert format_bytes(None) == "None"


# ============================================================
# clean_dragged_path
# ============================================================

def test_clean_dragged_path_empty():
    assert clean_dragged_path("") == ""
    assert clean_dragged_path(None) == ""


def test_clean_dragged_path_strips_matching_quotes():
    assert clean_dragged_path('"C:\\Users\\me\\file.mp4"') == "C:\\Users\\me\\file.mp4"
    assert clean_dragged_path("'/home/me/file.mp4'") == "/home/me/file.mp4"


def test_clean_dragged_path_unescapes_dragged_spaces():
    assert clean_dragged_path("/home/me/My\\ Folder/file.mp4") == "/home/me/My Folder/file.mp4"


def test_clean_dragged_path_mismatched_quotes_not_stripped():
    # Starts with a quote but doesn't end with one -> left alone (after strip)
    assert clean_dragged_path('"unterminated') == '"unterminated'


# ============================================================
# slugify_filename
# ============================================================

def test_slugify_filename_basic():
    assert slugify_filename(1, "My Cool Video") == "01_My_Cool_Video"


def test_slugify_filename_strips_special_chars():
    assert slugify_filename(3, "Weird!@# Name???") == "03_Weird_Name"


def test_slugify_filename_empty_title_falls_back_to_output():
    assert slugify_filename(5, "") == "05_output"
    assert slugify_filename(5, "!!!") == "05_output"


def test_slugify_filename_pads_index_to_two_digits():
    assert slugify_filename(7, "clip").startswith("07_")
    assert slugify_filename(123, "clip").startswith("123_")


# ============================================================
# get_album_folder_name
# ============================================================

def test_get_album_folder_name_basic():
    assert get_album_folder_name(101, "My Album") == "#101_My_Album"


def test_get_album_folder_name_strips_filesystem_illegal_chars():
    assert get_album_folder_name(1, 'Weird: "Title" <2024>') == "#1_Weird_Title_2024"


def test_get_album_folder_name_none_title_falls_back():
    assert get_album_folder_name(9, None) == "#9_unknown_album"


def test_get_album_folder_name_whitespace_only_title_falls_back():
    assert get_album_folder_name(9, "   ") == "#9_unknown_album"


# ============================================================
# extract_expiry_from_url
# ============================================================

def test_extract_expiry_from_url_present():
    url = "https://cdn.example.com/file.mp4?n=file.mp4&token=abc&ex=1893456000"
    assert extract_expiry_from_url(url) == 1893456000


def test_extract_expiry_from_url_missing_param():
    url = "https://cdn.example.com/file.mp4?n=file.mp4&token=abc"
    assert extract_expiry_from_url(url) is None


def test_extract_expiry_from_url_none_or_empty():
    assert extract_expiry_from_url(None) is None
    assert extract_expiry_from_url("") is None


def test_extract_expiry_from_url_malformed_ex_value_is_swallowed():
    """A non-numeric ex value hits the except clause and returns None
    rather than raising.
    """
    url = "https://cdn.example.com/file.mp4?ex=not_a_number"
    assert extract_expiry_from_url(url) is None


# ============================================================
# parse_and_check_expiry
# ============================================================

def test_parse_and_check_expiry_none():
    assert "No token found" in parse_and_check_expiry(None)
    assert "No token found" in parse_and_check_expiry(0)


def test_parse_and_check_expiry_expired():
    with patch("bunkr_api.utils.formatting.time.time", return_value=2000):
        result = parse_and_check_expiry(1000)
    assert "Expired" in result


def test_parse_and_check_expiry_valid_with_hours_remaining():
    with patch("bunkr_api.utils.formatting.time.time", return_value=1000):
        result = parse_and_check_expiry(1000 + 2 * 3600 + 5 * 60)  # 2h5m left
    assert "2h 5m left" in result


def test_parse_and_check_expiry_valid_with_only_minutes_remaining():
    with patch("bunkr_api.utils.formatting.time.time", return_value=1000):
        result = parse_and_check_expiry(1000 + 10 * 60)  # 10m left, no full hour
    assert "10m left" in result
    assert "h " not in result  # confirms the no-hours branch, not "0h 10m"


# ============================================================
# parse_selection
# ============================================================

def test_parse_selection_all_and_empty():
    assert parse_selection("all", total_items=5) == [1, 2, 3, 4, 5]
    assert parse_selection("", total_items=5) == [1, 2, 3, 4, 5]
    assert parse_selection("ALL", total_items=3) == [1, 2, 3]


def test_parse_selection_single_indices():
    assert parse_selection("1,3,5", total_items=5) == [1, 3, 5]


def test_parse_selection_range():
    assert parse_selection("2-4", total_items=5) == [2, 3, 4]


def test_parse_selection_reversed_range_is_normalized():
    assert parse_selection("4-2", total_items=5) == [2, 3, 4]


def test_parse_selection_mixed_and_dedupes():
    assert parse_selection("1,2-4,4,5", total_items=5) == [1, 2, 3, 4, 5]


def test_parse_selection_out_of_range_values_silently_dropped_if_some_valid():
    """Only fully-invalid selections raise; if at least one index is valid,
    out-of-range extras are silently filtered rather than raising.
    """
    assert parse_selection("2,999", total_items=5) == [2]


def test_parse_selection_raises_when_entirely_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        parse_selection("999", total_items=5)


def test_parse_selection_raises_on_invalid_numeric_chunk():
    with pytest.raises(ValueError, match="Invalid numeric index"):
        parse_selection("abc", total_items=5)


def test_parse_selection_raises_on_invalid_range_chunk():
    with pytest.raises(ValueError, match="Invalid range format"):
        parse_selection("2-abc", total_items=5)


# ============================================================
# sanitize_filename_simple
# ============================================================

def test_sanitize_filename_simple_keeps_allowed_chars():
    assert sanitize_filename_simple("My File (2024)_v2.mp4") == "My File (2024)_v2.mp4"


def test_sanitize_filename_simple_strips_disallowed_chars():
    assert sanitize_filename_simple('Weird:/*?"<>|Name.mp4') == "WeirdName.mp4"


def test_sanitize_filename_simple_trims_whitespace():
    assert sanitize_filename_simple("  spaced.mp4  ") == "spaced.mp4"