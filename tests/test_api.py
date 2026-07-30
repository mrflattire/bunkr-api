from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api.api import BunkrAPI
from bunkr_api.config import DEFAULT_OUTPUT_DIR


@pytest.fixture
def api(tmp_path):
    """A BunkrAPI instance with all sub-engines replaced by mocks.

    The real __init__ still runs (so we exercise the actual wiring), but we
    immediately swap db/scraper/downloader/player for mocks so tests only
    cover the facade logic in api.py, not the engines themselves.

    NOTE: downloader/player methods that are now `await`ed in api.py
    (downloader.run, player.play_mpv, player.play_vlc, player.resolve_tokens_async)
    are plain MagicMock attributes by default and are NOT awaitable — each
    test that awaits them explicitly reassigns them to AsyncMock, matching
    the style already used for player.resolve_tokens_async.
    """
    instance = BunkrAPI(db_path=str(tmp_path / "test.db"))
    instance.db = MagicMock()
    instance.scraper = MagicMock()
    instance.downloader = MagicMock()
    instance.player = MagicMock()
    return instance


# ============================================================
# DATA RETRIEVAL
# ============================================================

def test_get_albums(api):
    api.db.get_all_albums.return_value = [{"id": 1, "title": "Test Album"}]

    result = api.get_albums()

    assert result == [{"id": 1, "title": "Test Album"}]


def test_get_assets(api):
    api.db.get_album_assets.return_value = [{"id": 5, "title": "asset.mp4"}]

    result = api.get_assets(album_id=1)

    assert result == [{"id": 5, "title": "asset.mp4"}]
    api.db.get_album_assets.assert_called_once_with(1)


def test_get_album_found(api):
    api.db.get_album.return_value = {"id": 1, "title": "Test Album"}

    result = api.get_album(1)

    assert result == {"id": 1, "title": "Test Album"}


def test_get_album_not_found_returns_none(api):
    api.db.get_album.return_value = None

    assert api.get_album(999) is None


def test_get_valid_url_passes_through_to_db(api):
    api.db.get_valid_url.return_value = "https://cdn.example.com/fresh"

    result = api.get_valid_url(42)

    assert result == "https://cdn.example.com/fresh"
    api.db.get_valid_url.assert_called_once_with(42)


# ============================================================
# SEARCH
# ============================================================

@pytest.mark.asyncio
async def test_search_no_term(api):
    """No search term: URL should just be the base site, no urlencode needed."""
    mock_response = MagicMock()
    mock_response.text = "<html></html>"

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = AsyncMock(return_value=mock_response)

    api.scraper.parse_albums.return_value = [{"title": "Album"}]

    with patch("bunkr_api.api.AsyncSession", return_value=mock_session):
        result = await api.search(term="")

    assert result == [{"title": "Album"}]
    called_url = mock_session.get.call_args[0][0]
    assert called_url == "https://balbums.st/"


@pytest.mark.asyncio
async def test_search_with_term(api):
    """A non-empty term goes through urllib.parse.urlencode to build the query string."""
    mock_response = MagicMock()
    mock_response.text = "<html></html>"

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = AsyncMock(return_value=mock_response)

    api.scraper.parse_albums.return_value = [{"title": "Found Album"}]

    with patch("bunkr_api.api.AsyncSession", return_value=mock_session):
        result = await api.search(term="cats", mode="broad", per=10, sort="latest")

    assert result == [{"title": "Found Album"}]
    called_url = mock_session.get.call_args[0][0]
    assert called_url.startswith("https://balbums.st/?")
    assert "search=cats" in called_url


# ============================================================
# RESOLUTION
# ============================================================

@pytest.mark.asyncio
async def test_resolve_album(api):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)

    api.scraper.scrape_album = AsyncMock(return_value=42)

    with patch("bunkr_api.api.AsyncSession", return_value=mock_session):
        result = await api.resolve_album(
            "https://bunkr.si/a/xyz", search_context="ctx", save_json=True
        )

    assert result == 42
    api.scraper.scrape_album.assert_awaited_once_with(
        session=mock_session,
        url="https://bunkr.si/a/xyz",
        search_term="ctx",
        save_json=True,
    )


@pytest.mark.asyncio
async def test_resolve_and_download_chains_resolve_then_download(api):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    api.scraper.scrape_album = AsyncMock(return_value=7)

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = {"title": "Chained Album"}
    api.db.connection.return_value.__enter__.return_value = mock_conn
    api.db.connection.return_value.__exit__.return_value = False
    api.db.get_album_assets.return_value = []
    api.downloader.run = AsyncMock()

    with patch("bunkr_api.api.AsyncSession", return_value=mock_session):
        result = await api.resolve_and_download("https://bunkr.si/a/xyz", workers=2)

    assert result == 7
    api.scraper.scrape_album.assert_awaited_once()
    api.downloader.run.assert_awaited_once()
    assert api.downloader.run.call_args[1]["workers"] == 2


# ============================================================
# DOWNLOAD (download_album is now async)
# ============================================================

@pytest.mark.asyncio
async def test_download_album_not_found(api):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None
    api.db.connection.return_value.__enter__.return_value = mock_conn
    api.db.connection.return_value.__exit__.return_value = False

    with pytest.raises(ValueError):
        await api.download_album(album_id=999)


@pytest.mark.asyncio
async def test_download_album_success(api, tmp_path):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = {"title": "My Album"}
    api.db.connection.return_value.__enter__.return_value = mock_conn
    api.db.connection.return_value.__exit__.return_value = False

    api.db.get_album_assets.return_value = [
        {"id": 10, "title": "vid1.mp4"},
        {"id": 11, "title": "vid2.mp4"},
    ]
    api.downloader.run = AsyncMock()

    await api.download_album(album_id=1, workers=5, output_dir=tmp_path)

    api.downloader.run.assert_awaited_once()
    dl_list, kwargs = api.downloader.run.call_args[0][0], api.downloader.run.call_args[1]

    assert dl_list[0]["db_asset_id"] == 10
    assert dl_list[0]["album_title"] == "My Album"
    assert dl_list[0]["album_id"] == 1
    assert kwargs["workers"] == 5
    assert kwargs["output_dir"] == tmp_path


@pytest.mark.asyncio
async def test_download_staged_uses_get_staged_assets(api, tmp_path):
    api.db.get_staged_assets.return_value = [
        {"id": 20, "title": "staged.mp4", "album_title": "Some Album"},
    ]
    api.downloader.run = AsyncMock()

    await api.download_staged(workers=2, output_dir=tmp_path)

    api.db.get_staged_assets.assert_called_once()
    dl_list = api.downloader.run.call_args[0][0]
    assert dl_list[0]["db_asset_id"] == 20
    assert dl_list[0]["album_title"] == "Some Album"


@pytest.mark.asyncio
async def test_download_staged_no_op_when_nothing_staged(api, tmp_path):
    api.db.get_staged_assets.return_value = []
    api.downloader.run = AsyncMock()

    await api.download_staged()

    api.downloader.run.assert_awaited_once_with([], workers=3, output_dir=DEFAULT_OUTPUT_DIR)


@pytest.mark.asyncio
async def test_retry_failed_uses_get_failed_assets(api, tmp_path):
    api.db.get_failed_assets.return_value = [
        {"id": 30, "title": "failed.mp4", "album_title": "Some Album"},
    ]
    api.downloader.run = AsyncMock()

    await api.retry_failed(workers=4, output_dir=tmp_path)

    api.db.get_failed_assets.assert_called_once()
    dl_list, kwargs = api.downloader.run.call_args[0][0], api.downloader.run.call_args[1]
    assert dl_list[0]["db_asset_id"] == 30
    assert kwargs["workers"] == 4


# ============================================================
# STREAMING (stream_album is now async)
# ============================================================

@pytest.mark.asyncio
async def test_stream_album_no_assets(api):
    api.db.get_album_assets.return_value = []

    with pytest.raises(ValueError):
        await api.stream_album(album_id=99)


@pytest.mark.asyncio
async def test_stream_album_success(api):
    api.db.get_album_assets.return_value = [
        {"id": 1, "title": "a.mp4"},
        {"id": 2, "title": "b.mp4"},
    ]
    api.player.resolve_tokens_async = AsyncMock()
    api.player.play_mpv = AsyncMock()
    api.db.get_valid_url.side_effect = ["http://a", "http://b"]

    # parse_selection is imported locally inside stream_album (from
    # .utils.formatting import parse_selection), which re-fetches the name
    # from the source module on every call — so we patch it there, not on
    # bunkr_api.api.
    with patch(
        "bunkr_api.utils.formatting.parse_selection", return_value=[1, 2]
    ):
        await api.stream_album(album_id=1, indices_spec="all", player="mpv")

    api.player.resolve_tokens_async.assert_awaited_once()
    api.player.play_mpv.assert_awaited_once()
    queue_arg = api.player.play_mpv.call_args[0][0]
    assert queue_arg == [(1, "a.mp4", "http://a"), (2, "b.mp4", "http://b")]


@pytest.mark.asyncio
async def test_stream_album_uses_vlc_when_requested(api):
    api.db.get_album_assets.return_value = [{"id": 1, "title": "a.mp4"}]
    api.player.resolve_tokens_async = AsyncMock()
    api.player.play_vlc = AsyncMock()
    api.player.play_mpv = AsyncMock()
    api.db.get_valid_url.return_value = "http://a"

    with patch(
        "bunkr_api.utils.formatting.parse_selection", return_value=[1]
    ):
        await api.stream_album(album_id=1, indices_spec="1", player="vlc")

    api.player.play_vlc.assert_awaited_once()
    api.player.play_mpv.assert_not_awaited()


# ============================================================
# MAINTENANCE (refresh_tokens no longer uses daemon_loop at all —
# it now calls refresh_all_tokens_async directly, same as the CLI's
# "mint now" action after the async refactor)
# ============================================================

@pytest.mark.asyncio
async def test_refresh_tokens_targeted_refreshes_pending_assets(api):
    api.db.get_needs_refresh.return_value = [{"id": 1, "true_file_id": "abc"}]
    api.db.get_config_val.return_value = "4"

    with patch(
        "bunkr_api.api.refresh_all_tokens_async", new_callable=AsyncMock
    ) as mock_refresh:
        await api.refresh_tokens(album_id=5)

    api.db.get_needs_refresh.assert_called_once_with(album_id=5)
    mock_refresh.assert_awaited_once_with(api.db, [{"id": 1, "true_file_id": "abc"}], 4)


@pytest.mark.asyncio
async def test_refresh_tokens_skips_call_when_nothing_needs_refresh(api):
    api.db.get_needs_refresh.return_value = []

    with patch(
        "bunkr_api.api.refresh_all_tokens_async", new_callable=AsyncMock
    ) as mock_refresh:
        await api.refresh_tokens(album_id=5)

    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_tokens_no_album_id_passes_none_through(api):
    """With no album_id, refresh_tokens now does a genuine one-shot pass over
    the WHOLE database's due assets and returns — no more infinite daemon.
    """
    api.db.get_needs_refresh.return_value = [{"id": 2, "true_file_id": "xyz"}]
    api.db.get_config_val.return_value = "8"

    with patch(
        "bunkr_api.api.refresh_all_tokens_async", new_callable=AsyncMock
    ) as mock_refresh:
        await api.refresh_tokens()

    api.db.get_needs_refresh.assert_called_once_with(album_id=None)
    mock_refresh.assert_awaited_once_with(api.db, [{"id": 2, "true_file_id": "xyz"}], 8)


def test_delete_album_returns_db_result(api):
    api.db.delete_album.return_value = True

    result = api.delete_album(5)

    assert result is True
    api.db.delete_album.assert_called_once_with(5)


def test_delete_album_false_when_nothing_deleted(api):
    api.db.delete_album.return_value = False

    assert api.delete_album(999) is False