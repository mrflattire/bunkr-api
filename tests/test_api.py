from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api.api import BunkrAPI


@pytest.fixture
def api(tmp_path):
    """A BunkrAPI instance with all sub-engines replaced by mocks.

    The real __init__ still runs (so we exercise the actual wiring), but we
    immediately swap db/scraper/downloader/player for mocks so tests only
    cover the facade logic in api.py, not the engines themselves.
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
    """A non-empty term goes through urllib.parse.urlencode to build the query string.

    NOTE: this currently fails with NameError: name 'urllib' is not defined,
    because api.py uses urllib.parse.urlencode without importing urllib.
    """
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


# ============================================================
# DOWNLOAD
# ============================================================

def test_download_album_not_found(api):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None
    api.db.connection.return_value.__enter__.return_value = mock_conn
    api.db.connection.return_value.__exit__.return_value = False

    with pytest.raises(ValueError):
        api.download_album(album_id=999)


def test_download_album_success(api, tmp_path):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = {"title": "My Album"}
    api.db.connection.return_value.__enter__.return_value = mock_conn
    api.db.connection.return_value.__exit__.return_value = False

    api.db.get_album_assets.return_value = [
        {"id": 10, "title": "vid1.mp4"},
        {"id": 11, "title": "vid2.mp4"},
    ]

    api.download_album(album_id=1, workers=5, output_dir=tmp_path)

    api.downloader.run.assert_called_once()
    dl_list, kwargs = api.downloader.run.call_args[0][0], api.downloader.run.call_args[1]

    assert dl_list[0]["db_asset_id"] == 10
    assert dl_list[0]["album_title"] == "My Album"
    assert dl_list[0]["album_id"] == 1
    assert kwargs["workers"] == 5
    assert kwargs["output_dir"] == tmp_path


# ============================================================
# STREAMING
# ============================================================

def test_stream_album_no_assets(api):
    api.db.get_album_assets.return_value = []

    with pytest.raises(ValueError):
        api.stream_album(album_id=99)


def test_stream_album_success(api):
    api.db.get_album_assets.return_value = [
        {"id": 1, "title": "a.mp4"},
        {"id": 2, "title": "b.mp4"},
    ]
    api.player.resolve_tokens_async = AsyncMock()
    api.db.get_valid_url.side_effect = ["http://a", "http://b"]

    # parse_selection is imported locally inside stream_album (from
    # .utils.formatting import parse_selection), which re-fetches the name
    # from the source module on every call — so we patch it there, not on
    # bunkr_api.api.
    with patch(
        "bunkr_api.utils.formatting.parse_selection", return_value=[1, 2]
    ):
        api.stream_album(album_id=1, indices_spec="all", player="mpv")

    api.player.resolve_tokens_async.assert_awaited_once()
    api.player.play_mpv.assert_called_once()
    queue_arg = api.player.play_mpv.call_args[0][0]
    assert queue_arg == [(1, "a.mp4", "http://a"), (2, "b.mp4", "http://b")]


def test_stream_album_uses_vlc_when_requested(api):
    api.db.get_album_assets.return_value = [{"id": 1, "title": "a.mp4"}]
    api.player.resolve_tokens_async = AsyncMock()
    api.db.get_valid_url.return_value = "http://a"

    with patch(
        "bunkr_api.utils.formatting.parse_selection", return_value=[1]
    ):
        api.stream_album(album_id=1, indices_spec="1", player="vlc")

    api.player.play_vlc.assert_called_once()
    api.player.play_mpv.assert_not_called()


# ============================================================
# MAINTENANCE
# ============================================================

def test_refresh_tokens_targeted(api):
    """With an album_id, refresh_tokens delegates to daemon_loop's one-shot path."""
    with patch("bunkr_api.api.daemon_loop") as mock_daemon:
        api.refresh_tokens(album_id=5)

    mock_daemon.assert_called_once_with(album_id=5)


def test_refresh_tokens_no_id_launches_daemon(api):
    """With no album_id, refresh_tokens hands off to daemon_loop's polling
    mode. daemon_loop itself is mocked here since its unbounded `while True`
    loop is exercised separately in core/tokens tests, not here — this test
    only pins down that the facade passes album_id=None through unchanged
    rather than silently defaulting to some other one-shot behavior.
    """
    with patch("bunkr_api.api.daemon_loop") as mock_daemon:
        api.refresh_tokens()

    mock_daemon.assert_called_once_with(album_id=None)