import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api.core.scraper import ScraperEngine

# ============================================================
# NOTE ON PATCHING TARGETS
# ============================================================
# Both `execute_request_with_retry_async` and `slugify_filename` are
# imported at the TOP of scraper.py, so they're patched where they're
# looked up: "bunkr_api.core.scraper.<name>".
# ============================================================


# ============================================================
# _safe_int
# ============================================================


@pytest.mark.parametrize(
    "value, expected",
    [
        (42, 42),
        (0, 0),
        (None, 0),
        ("", 0),
        ("123", 123),
        ("1.82 GB", 182),  # strips everything but digits
        ("1,024", 1024),
        ("no digits here", 0),
    ],
)
def test_safe_int(value, expected):
    engine = ScraperEngine(db=MagicMock())
    assert engine._safe_int(value) == expected


# ============================================================
# standardize_top_url
# ============================================================


def test_standardize_top_url_converts_v_and_i_to_f():
    engine = ScraperEngine(db=MagicMock())
    assert engine.standardize_top_url("https://bunkr.cr/v/abc123") == "https://bunkr.cr/f/abc123"
    assert engine.standardize_top_url("https://bunkr.cr/i/xyz789") == "https://bunkr.cr/f/xyz789"


def test_standardize_top_url_leaves_other_urls_unchanged():
    engine = ScraperEngine(db=MagicMock())
    url = "https://bunkr.cr/a/some-album"
    assert engine.standardize_top_url(url) == url


# ============================================================
# parse_albums
# ============================================================

ALBUMS_HTML = """
<div>
  <a href="/a/abc123">
    <h3>My Album</h3>
    <span>10 files</span>
  </a>
  <a href="/a/xyz789">
    <h3>Another Album</h3>
    <span>Unrelated text</span>
  </a>
  <a href="/a/abc123">
    <h3>Duplicate Of First</h3>
  </a>
  <a href="/v/not-an-album">
    <h3>Not An Album</h3>
  </a>
</div>
"""


def test_parse_albums_extracts_title_url_and_file_count():
    engine = ScraperEngine(db=MagicMock())
    albums = engine.parse_albums(ALBUMS_HTML)

    titles = {a["title"]: a for a in albums}
    assert "My Album" in titles
    assert titles["My Album"]["url"] == "https://bunkr.cr/a/abc123"
    assert titles["My Album"]["file_count"] == "10 files"


def test_parse_albums_defaults_file_count_when_no_file_span(temp_db=None):
    engine = ScraperEngine(db=MagicMock())
    albums = engine.parse_albums(ALBUMS_HTML)
    titles = {a["title"]: a for a in albums}
    assert titles["Another Album"]["file_count"] == "0 files"


def test_parse_albums_deduplicates_by_url():
    engine = ScraperEngine(db=MagicMock())
    albums = engine.parse_albums(ALBUMS_HTML)
    urls = [a["url"] for a in albums]
    assert len(urls) == len(set(urls))


def test_parse_albums_ignores_non_album_links():
    engine = ScraperEngine(db=MagicMock())
    albums = engine.parse_albums(ALBUMS_HTML)
    assert all("/a/" in a["url"] for a in albums)


def test_parse_albums_empty_html_returns_empty_list():
    engine = ScraperEngine(db=MagicMock())
    assert engine.parse_albums("<html><body></body></html>") == []


# ============================================================
# parse_top_items
# ============================================================

TOP_VIDEOS_HTML = """
<div>
  <a href="/v/vid123">
    <h3>Cool Video</h3>
    <span>1 file</span>
  </a>
  <a href="/v/vid456">
    <p>Fallback Title Video</p>
  </a>
</div>
"""


def test_parse_top_items_videos_standardizes_url_to_f():
    engine = ScraperEngine(db=MagicMock())
    items = engine.parse_top_items(TOP_VIDEOS_HTML, category="videos")
    urls = {i["title"]: i["url"] for i in items}
    assert urls["Cool Video"] == "https://bunkr.cr/f/vid123"


def test_parse_top_items_falls_back_to_p_tag_for_title():
    engine = ScraperEngine(db=MagicMock())
    items = engine.parse_top_items(TOP_VIDEOS_HTML, category="videos")
    titles = [i["title"] for i in items]
    assert "Fallback Title Video" in titles


def test_parse_top_items_defaults_file_count_to_one_file():
    engine = ScraperEngine(db=MagicMock())
    items = engine.parse_top_items(TOP_VIDEOS_HTML, category="videos")
    by_title = {i["title"]: i for i in items}
    assert by_title["Fallback Title Video"]["file_count"] == "1 file"


def test_parse_top_items_albums_category_does_not_standardize_url():
    html = '<a href="/a/album1"><h3>An Album</h3></a>'
    engine = ScraperEngine(db=MagicMock())
    items = engine.parse_top_items(html, category="albums")
    assert items[0]["url"] == "https://bunkr.cr/a/album1"


# ============================================================
# parse_album_header_metadata
# ============================================================


def _soup(html):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def test_parse_album_header_metadata_extracts_size_and_count():
    engine = ScraperEngine(db=MagicMock())
    html = '<div class="visitors"><span class="font-semibold">(8.84 GB) 93 files</span></div>'
    size, count = engine.parse_album_header_metadata(_soup(html))
    assert size == "8.84 GB"
    assert count == "93 files"


def test_parse_album_header_metadata_missing_element_returns_none_none():
    engine = ScraperEngine(db=MagicMock())
    size, count = engine.parse_album_header_metadata(_soup("<div></div>"))
    assert size is None
    assert count is None


def test_parse_album_header_metadata_no_parens_leaves_size_none(temp_db=None):
    engine = ScraperEngine(db=MagicMock())
    html = '<div class="visitors"><span class="font-semibold">no parens here</span></div>'
    size, count = engine.parse_album_header_metadata(_soup(html))
    assert size is None
    assert count is None  # files_match also needs a ')' to anchor on


# ============================================================
# extract_advanced_album_files
# ============================================================


def test_extract_advanced_album_files_parses_full_object():
    engine = ScraperEngine(db=MagicMock())
    html = (
        '<script>window.albumFiles = [{id: 1, name: "raw_name.mp4", '
        'slug: "clean-slug", original: "Pretty Name.mp4", size: 1048576}];</script>'
    )
    files = engine.extract_advanced_album_files(html)

    assert len(files) == 1
    f = files[0]
    assert f["true_file_id"] == 1
    assert f["slug_id"] == 1
    assert f["href"] == "https://bunkr.cr/f/clean-slug"
    assert f["title"] == "Pretty Name.mp4"
    assert f["original"] == "Pretty Name.mp4"
    assert f["size"] == 1048576


def test_extract_advanced_album_files_falls_back_to_name_for_slug_and_title():
    engine = ScraperEngine(db=MagicMock())
    html = '<script>window.albumFiles = [{id: 2, name: "fallback.mp4", size: 512}];</script>'
    files = engine.extract_advanced_album_files(html)

    assert files[0]["href"] == "https://bunkr.cr/f/fallback.mp4"
    assert files[0]["title"] == "fallback.mp4"


def test_extract_advanced_album_files_no_match_returns_empty_list():
    engine = ScraperEngine(db=MagicMock())
    assert engine.extract_advanced_album_files("<html>nothing here</html>") == []


def test_extract_advanced_album_files_multiple_objects():
    engine = ScraperEngine(db=MagicMock())
    html = (
        "<script>window.albumFiles = ["
        '{id: 1, name: "a.mp4", size: 100}, '
        '{id: 2, name: "b.mp4", size: 200}'
        "];</script>"
    )
    files = engine.extract_advanced_album_files(html)
    assert len(files) == 2
    assert {f["true_file_id"] for f in files} == {1, 2}


def test_extract_advanced_album_files_missing_size_defaults_to_zero():
    engine = ScraperEngine(db=MagicMock())
    html = '<script>window.albumFiles = [{id: 3, name: "no_size.mp4"}];</script>'
    files = engine.extract_advanced_album_files(html)
    assert files[0]["size"] == 0


# ============================================================
# scrape_album
# ============================================================

SAMPLE_ALBUM_HTML = """
<html>
<head><title>My Great Album - Bunkr</title></head>
<body>
  <div class="visitors"><span class="font-semibold">(1.82 GB) 1 files</span></div>
  <script>
  window.albumFiles = [{id: 1, name: "a.mp4", slug: "a-slug", original: "A Original.mp4", size: 1048576}];
  </script>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_scrape_album_builds_optimized_url_and_registers(temp_db):
    engine = ScraperEngine(temp_db)
    temp_db.register_album_from_json = MagicMock(return_value=99)
    mock_session = AsyncMock()
    mock_response = MagicMock()
    mock_response.text = SAMPLE_ALBUM_HTML

    with patch(
        "bunkr_api.core.scraper.execute_request_with_retry_async",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_fetch:
        album_id = await engine.scrape_album(
            mock_session,
            "https://bunkr.cr/a/xyz?foo=bar",
            search_term="test",
            album_number_index=1,
        )

    assert album_id == 99
    called_url = mock_fetch.call_args[0][1]
    assert "advanced=1" in called_url
    assert "foo=bar" in called_url  # existing query params preserved

    data_arg = temp_db.register_album_from_json.call_args[0][0]
    assert data_arg["selected_album"]["title"] == "My Great Album"
    assert data_arg["selected_album"]["aggregate_size"] == "1.82 GB"
    assert len(data_arg["files_found"]) == 1
    assert data_arg["files_found"][0]["true_file_id"] == 1


@pytest.mark.asyncio
async def test_scrape_album_save_json_writes_file(temp_db, tmp_path):
    engine = ScraperEngine(temp_db)
    temp_db.register_album_from_json = MagicMock(return_value=5)
    mock_session = AsyncMock()
    mock_response = MagicMock()
    mock_response.text = SAMPLE_ALBUM_HTML

    with patch(
        "bunkr_api.core.scraper.execute_request_with_retry_async",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        await engine.scrape_album(
            mock_session,
            "https://bunkr.cr/a/xyz",
            search_term="t",
            album_number_index=2,
            save_json=True,
            output_dir=tmp_path,
        )

    json_files = list(tmp_path.glob("*.json"))
    assert len(json_files) == 1
    saved_data = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert saved_data["selected_album"]["title"] == "My Great Album"


@pytest.mark.asyncio
async def test_scrape_album_save_json_failure_does_not_raise(temp_db, tmp_path):
    engine = ScraperEngine(temp_db)
    temp_db.register_album_from_json = MagicMock(return_value=5)
    mock_session = AsyncMock()
    mock_response = MagicMock()
    mock_response.text = SAMPLE_ALBUM_HTML

    with (
        patch(
            "bunkr_api.core.scraper.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch(
            "bunkr_api.core.scraper.asyncio.to_thread",
            side_effect=OSError("disk full"),
        ),
    ):
        # Should not raise despite the write failure — caught and logged.
        album_id = await engine.scrape_album(
            mock_session,
            "https://bunkr.cr/a/xyz",
            search_term="t",
            save_json=True,
            output_dir=tmp_path,
        )

    assert album_id == 5


@pytest.mark.asyncio
async def test_scrape_album_defaults_when_header_metadata_missing(temp_db):
    engine = ScraperEngine(temp_db)
    temp_db.register_album_from_json = MagicMock(return_value=1)
    mock_session = AsyncMock()
    mock_response = MagicMock()
    mock_response.text = "<html><head><title>No Meta - Bunkr</title></head><body></body></html>"

    with patch(
        "bunkr_api.core.scraper.execute_request_with_retry_async",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        await engine.scrape_album(mock_session, "https://bunkr.cr/a/xyz", search_term="t")

    data_arg = temp_db.register_album_from_json.call_args[0][0]
    assert data_arg["selected_album"]["aggregate_size"] == "0 MB"
    assert data_arg["selected_album"]["clean_file_count"] == "0 files"
    assert data_arg["files_found"] == []
