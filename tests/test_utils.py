import pytest
from bunkr_api.utils.formatting import format_bytes, slugify_filename


@pytest.mark.parametrize(
    ("index", "name", "expected"),
    [
        (1, "Hello World!", "01_Hello_World"),
        (99, "Spaces   And Symbols!!!", "99_Spaces_And_Symbols"),
        (5, "Special / Slash : Test", "05_Special_Slash_Test"),
    ],
)
def test_slugify(index, name, expected):
    assert slugify_filename(index, name) == expected


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0.00 B"),
        (1024, "1.00 KB"),
        (1024 * 1024, "1.00 MB"),
        (1024 * 1024 * 1024, "1.00 GB"),
    ],
)
def test_format_bytes(size_bytes, expected):
    assert format_bytes(size_bytes) == expected