import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.table import Table

from bunkr_api.cli import (
    _run,
    main,
    main_loop,
    run_scrape_interactive,
    run_top_engine_interactive,
    show_album_details,
    show_interactive_options,
)

# ============================================================
# NOTE ON THE MODULE-LEVEL `db`
# ============================================================
# cli.py does `db = DatabaseManager()` at IMPORT time, at module scope —
# every function in this module reads/writes that bare global, not an
# injected parameter. So instead of constructing BunkrAPI-style instances
# per test, every test here patches "bunkr_api.cli.db" directly via the
# autouse `mock_db` fixture below.
#
# NOTE ON LOCAL IMPORTS
# `execute_request_with_retry_async` is imported LOCALLY inside
# run_scrape_interactive/run_top_engine_interactive
# (`from .utils.http import execute_request_with_retry_async`), so it's
# patched at its source: "bunkr_api.utils.http.execute_request_with_retry_async".
# `AsyncSession` and `ScraperEngine`/`DownloadEngine`/`PlayerEngine` are all
# imported at the TOP of cli.py, so they're patched where they're looked up:
# "bunkr_api.cli.<name>".
# ============================================================


@pytest.fixture(autouse=True)
def mock_db():
    with patch("bunkr_api.cli.db") as mock:
        yield mock


@pytest.fixture
def fake_album():
    return {
        "id": 1,
        "title": "Test Album",
        "search_term": "query",
        "global_index": 5,
        "aggregate_size": "1.82 GB",
        "file_count": 2,
        "is_staged": 0,
    }


@pytest.fixture
def fake_assets():
    return [
        {
            "id": 10,
            "title": "a.mp4",
            "raw_size_bytes": 1000,
            "token_expiry_timestamp": None,
            "signed_cdn_url": "http://a",
            "source_url": "http://src/a",
            "is_staged": 0,
        },
        {
            "id": 11,
            "title": "b.mp4",
            "raw_size_bytes": 2000,
            "token_expiry_timestamp": None,
            "signed_cdn_url": "http://b",
            "source_url": "http://src/b",
            "is_staged": 0,
        },
    ]


def _wire_db_for_album(mock_db, fake_album, fake_assets):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = fake_album
    mock_db.connection.return_value.__enter__.return_value = mock_conn
    mock_db.connection.return_value.__exit__.return_value = False
    mock_db.get_album_assets.return_value = fake_assets
    return mock_conn


# ============================================================
# show_interactive_options
# ============================================================


def test_show_interactive_options_returns_choice():
    with patch("bunkr_api.cli.Prompt.ask", return_value="2"):
        result = show_interactive_options(
            album_id=1,
            page_assets=[],
            start_idx=0,
            total_pages=1,
            current_page=1,
            total_items=0,
        )
    assert result == "2"


def test_show_interactive_options_shows_expired_warning(capsys):
    expired_asset = {"token_expiry_timestamp": 1}
    with (
        patch("bunkr_api.cli.parse_and_check_expiry", return_value="[bold red]Expired[/bold red]"),
        patch("bunkr_api.cli.Prompt.ask", return_value="q"),
    ):
        show_interactive_options(
            album_id=1,
            page_assets=[expired_asset],
            start_idx=0,
            total_pages=1,
            current_page=1,
            total_items=1,
        )
    captured = capsys.readouterr()
    assert "EXPIRED" in captured.out


def test_show_interactive_options_no_warning_when_all_valid(capsys):
    valid_asset = {"token_expiry_timestamp": 9999999999}
    with (
        patch(
            "bunkr_api.cli.parse_and_check_expiry", return_value="[bold green]Valid[/bold green]"
        ),
        patch("bunkr_api.cli.Prompt.ask", return_value="q"),
    ):
        show_interactive_options(
            album_id=1,
            page_assets=[valid_asset],
            start_idx=0,
            total_pages=1,
            current_page=1,
            total_items=1,
        )
    captured = capsys.readouterr()
    assert "EXPIRED" not in captured.out


# ============================================================
# show_album_details
# ============================================================


@pytest.mark.asyncio
async def test_show_album_details_album_not_found(mock_db, capsys):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None
    mock_db.connection.return_value.__enter__.return_value = mock_conn
    mock_db.connection.return_value.__exit__.return_value = False

    await show_album_details(album_id=999)

    assert "missing" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_show_album_details_quit_immediately(mock_db, fake_album, fake_assets):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    with patch("bunkr_api.cli.Prompt.ask", return_value="q"):
        await show_album_details(album_id=1)  # should return, not hang


@pytest.mark.asyncio
async def test_show_album_details_pagination_bounds(mock_db, fake_album):
    many_assets = [
        {
            "id": i,
            "title": f"f{i}.mp4",
            "raw_size_bytes": 100,
            "token_expiry_timestamp": None,
            "signed_cdn_url": None,
            "source_url": None,
            "is_staged": 0,
        }
        for i in range(1, 16)  # 15 assets -> 2 pages at page_size=10
    ]
    _wire_db_for_album(mock_db, fake_album, many_assets)

    with patch("bunkr_api.cli.Prompt.ask", side_effect=["n", "p", "p", "q"]) as mock_ask:
        await show_album_details(album_id=1)

    assert mock_ask.call_count == 4


@pytest.mark.asyncio
async def test_show_album_details_stream_option(mock_db, fake_album, fake_assets):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    mock_db.get_valid_url.side_effect = ["http://resolved-a", "http://resolved-b"]

    mock_player = MagicMock()
    mock_player.resolve_tokens_async = AsyncMock()
    mock_player.play_mpv = AsyncMock()

    with (
        patch("bunkr_api.cli.PlayerEngine", return_value=mock_player),
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["1", "", "mpv", "q"]),
    ):
        await show_album_details(album_id=1)

    mock_player.resolve_tokens_async.assert_awaited_once()
    mock_player.play_mpv.assert_awaited_once()
    queue_arg = mock_player.play_mpv.call_args[0][0]
    assert queue_arg == [(1, "a.mp4", "http://resolved-a"), (2, "b.mp4", "http://resolved-b")]


@pytest.mark.asyncio
async def test_show_album_details_stream_uses_vlc_when_requested(mock_db, fake_album, fake_assets):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    mock_db.get_valid_url.return_value = "http://x"

    mock_player = MagicMock()
    mock_player.resolve_tokens_async = AsyncMock()
    mock_player.play_vlc = AsyncMock()
    mock_player.play_mpv = AsyncMock()

    with (
        patch("bunkr_api.cli.PlayerEngine", return_value=mock_player),
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["1", "1", "vlc", "q"]),
    ):
        await show_album_details(album_id=1)

    mock_player.play_vlc.assert_awaited_once()
    mock_player.play_mpv.assert_not_awaited()


@pytest.mark.asyncio
async def test_show_album_details_download_specific(mock_db, fake_album, fake_assets):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    mock_downloader = MagicMock()
    mock_downloader.run = AsyncMock()

    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=mock_downloader),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["2", "1", "q"]),
        patch("bunkr_api.cli.IntPrompt.ask", return_value=3),
    ):
        await show_album_details(album_id=1)

    mock_downloader.run.assert_awaited_once()
    dl_list, kwargs = mock_downloader.run.call_args[0][0], mock_downloader.run.call_args[1]
    assert len(dl_list) == 1
    assert dl_list[0]["db_asset_id"] == 10
    assert kwargs["workers"] == 3


@pytest.mark.asyncio
async def test_show_album_details_download_specific_blank_selection_skips(
    mock_db, fake_album, fake_assets
):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    mock_downloader = MagicMock()
    mock_downloader.run = AsyncMock()

    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=mock_downloader),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["2", "", "q"]),
    ):
        await show_album_details(album_id=1)

    mock_downloader.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_show_album_details_download_all(mock_db, fake_album, fake_assets):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    mock_downloader = MagicMock()
    mock_downloader.run = AsyncMock()

    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=mock_downloader),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["3", "q"]),
        patch("bunkr_api.cli.IntPrompt.ask", return_value=2),
    ):
        await show_album_details(album_id=1)

    dl_list = mock_downloader.run.call_args[0][0]
    assert len(dl_list) == len(fake_assets)


@pytest.mark.asyncio
async def test_show_album_details_copy_link_valid(mock_db, fake_album, fake_assets, capsys):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["4", "1", "", "q"]),
    ):
        await show_album_details(album_id=1)

    assert "http://a" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_show_album_details_copy_link_invalid_index(mock_db, fake_album, fake_assets, capsys):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["4", "not_a_number", "q"]),
    ):
        await show_album_details(album_id=1)

    assert "Invalid selection" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_show_album_details_mint_tokens_when_expiring(mock_db, fake_album, fake_assets):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    mock_db.get_needs_refresh.return_value = [{"id": 10}]
    mock_db.get_config_val.return_value = "4"

    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.refresh_all_tokens_async", new_callable=AsyncMock) as mock_refresh,
        patch("bunkr_api.cli.Prompt.ask", side_effect=["5", "q"]),
    ):
        await show_album_details(album_id=1)

    mock_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_show_album_details_mint_tokens_none_expiring(mock_db, fake_album, fake_assets):
    _wire_db_for_album(mock_db, fake_album, fake_assets)
    mock_db.get_needs_refresh.return_value = []

    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.refresh_all_tokens_async", new_callable=AsyncMock) as mock_refresh,
        patch("bunkr_api.cli.Prompt.ask", side_effect=["5", "q"]),
    ):
        await show_album_details(album_id=1)

    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_show_album_details_stage_album(mock_db, fake_album, fake_assets):
    mock_conn = _wire_db_for_album(mock_db, fake_album, fake_assets)

    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.time.sleep"),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["6", "1", "q"]),
    ):
        await show_album_details(album_id=1)

    executed_sql = [c.args[0] for c in mock_conn.execute.call_args_list]
    assert any("UPDATE albums SET is_staged=1" in sql for sql in executed_sql)
    assert any("UPDATE assets SET is_staged=1" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_show_album_details_stage_specific_assets(mock_db, fake_album, fake_assets):
    mock_conn = _wire_db_for_album(mock_db, fake_album, fake_assets)

    with (
        patch("bunkr_api.cli.DownloadEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.PlayerEngine", return_value=MagicMock()),
        patch("bunkr_api.cli.time.sleep"),
        patch("bunkr_api.cli.Prompt.ask", side_effect=["6", "3", "1", "q"]),
    ):
        await show_album_details(album_id=1)

    executed = [c.args for c in mock_conn.execute.call_args_list]
    assert any(
        args[0].startswith("UPDATE assets SET is_staged=?") and args[1] == (1, 10)
        for args in executed
    )


# ============================================================
# run_scrape_interactive
# ============================================================


@pytest.mark.asyncio
async def test_run_scrape_interactive_selects_album(mock_db):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_response = MagicMock()
    mock_response.text = "<html>fake</html>"

    with (
        patch("bunkr_api.cli.AsyncSession", return_value=mock_session),
        patch(
            "bunkr_api.utils.http.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch("bunkr_api.cli.ScraperEngine") as mock_scraper_cls,
        patch("bunkr_api.cli.Prompt.ask", return_value="1"),
    ):
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.parse_albums.return_value = [
            {"title": "Album A", "url": "https://x", "file_count": "5 files"}
        ]
        mock_scraper.scrape_album = AsyncMock(return_value=77)

        result = await run_scrape_interactive(
            search_seed="query",
            mode_seed="broad",
            per_seed=20,
            sort_seed="latest",
            save_json_seed=False,
        )

    assert result == 77
    mock_scraper.scrape_album.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scrape_interactive_shows_total_pages_and_mode_in_title(mock_db):
    """Regression test: extract_page_metadata()'s result and the search mode
    must appear in the table title (previously dropped during modularization).
    """
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_response = MagicMock()
    mock_response.text = "<html>fake</html>"

    printed = []

    with (
        patch("bunkr_api.cli.AsyncSession", return_value=mock_session),
        patch(
            "bunkr_api.utils.http.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch("bunkr_api.cli.ScraperEngine") as mock_scraper_cls,
        patch("bunkr_api.cli.Prompt.ask", return_value="q"),
        patch("bunkr_api.cli.console.print", side_effect=lambda *a, **k: printed.extend(a)),
    ):
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.parse_albums.return_value = [
            {"title": "Album A", "url": "https://x", "file_count": "5 files"}
        ]
        mock_scraper.extract_page_metadata.return_value = "3"

        await run_scrape_interactive(
            search_seed="Zishy",
            mode_seed="broad",
            per_seed=20,
            sort_seed="latest",
            save_json_seed=False,
        )

    mock_scraper.extract_page_metadata.assert_called_once()
    titles = [obj.title for obj in printed if isinstance(obj, Table)]
    assert any("Page 1 of 3" in t for t in titles)
    assert any("Mode: broad" in t for t in titles)
    assert any('"Zishy"' in t for t in titles)


@pytest.mark.asyncio
async def test_run_scrape_interactive_no_albums_returns_none(mock_db):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_response = MagicMock()
    mock_response.text = "<html></html>"

    with (
        patch("bunkr_api.cli.AsyncSession", return_value=mock_session),
        patch(
            "bunkr_api.utils.http.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch("bunkr_api.cli.ScraperEngine") as mock_scraper_cls,
    ):
        mock_scraper_cls.return_value.parse_albums.return_value = []

        result = await run_scrape_interactive(
            search_seed="query",
            mode_seed="broad",
            per_seed=20,
            sort_seed="latest",
            save_json_seed=False,
        )

    assert result is None


@pytest.mark.asyncio
async def test_run_scrape_interactive_quit_returns_none(mock_db):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_response = MagicMock()
    mock_response.text = "<html></html>"

    with (
        patch("bunkr_api.cli.AsyncSession", return_value=mock_session),
        patch(
            "bunkr_api.utils.http.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch("bunkr_api.cli.ScraperEngine") as mock_scraper_cls,
        patch("bunkr_api.cli.Prompt.ask", return_value="q"),
    ):
        mock_scraper_cls.return_value.parse_albums.return_value = [
            {"title": "A", "url": "https://x", "file_count": "1 file"}
        ]

        result = await run_scrape_interactive(
            search_seed="query",
            mode_seed="broad",
            per_seed=20,
            sort_seed="latest",
            save_json_seed=False,
        )

    assert result is None


# ============================================================
# run_top_engine_interactive
# ============================================================


@pytest.mark.asyncio
async def test_run_top_engine_interactive_selects_item(mock_db):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_response = MagicMock()
    mock_response.text = "<html></html>"

    with (
        patch("bunkr_api.cli.AsyncSession", return_value=mock_session),
        patch(
            "bunkr_api.utils.http.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch("bunkr_api.cli.ScraperEngine") as mock_scraper_cls,
        patch("bunkr_api.cli.Prompt.ask", side_effect=["24h", "1"]),
    ):
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.parse_top_items.return_value = [
            {"title": "Trend A", "url": "https://y", "file_count": "1 file"}
        ]
        mock_scraper.scrape_album = AsyncMock(return_value=88)

        result = await run_top_engine_interactive(category_seed="albums", save_json_seed=False)

    assert result == 88


@pytest.mark.asyncio
async def test_run_top_engine_interactive_no_items_returns_none(mock_db):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_response = MagicMock()
    mock_response.text = "<html></html>"

    with (
        patch("bunkr_api.cli.AsyncSession", return_value=mock_session),
        patch(
            "bunkr_api.utils.http.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=mock_response,
        ),
        patch("bunkr_api.cli.ScraperEngine") as mock_scraper_cls,
        patch("bunkr_api.cli.Prompt.ask", return_value="24h"),
    ):
        mock_scraper_cls.return_value.parse_top_items.return_value = []

        result = await run_top_engine_interactive(category_seed="albums", save_json_seed=False)

    assert result is None


# ============================================================
# _run() routing
#
# See the note above the test file: only --db-id and -i (the explicit flag
# forms) actually work for jumping straight to an album. The bare-positional
# convenience forms described in the code's own comments are dead code,
# confirmed empirically — a single bare token always lands in `args.search`
# (declared first), never in `args.path` (declared second), regardless of
# whether it looks like a number or a .json filename.
# ============================================================


@pytest.mark.asyncio
async def test_run_route_explicit_json_import(mock_db, tmp_path):
    json_file = tmp_path / "album.json"
    json_file.write_text(json.dumps({"selected_album": {}, "files_found": []}))
    mock_db.register_album_from_json.return_value = (42, 1, 0)

    with (
        patch("sys.argv", ["bunkr-api", "-i", str(json_file)]),
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as mock_show,
    ):
        await _run()

    mock_show.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_run_route_explicit_json_import_failure_exits(mock_db, tmp_path):
    json_file = tmp_path / "bad.json"
    json_file.write_text("not valid json{{{")

    with patch("sys.argv", ["bunkr-api", "-i", str(json_file)]), pytest.raises(SystemExit):
        await _run()


@pytest.mark.asyncio
async def test_run_route_explicit_db_id_flag(mock_db):
    with (
        patch("sys.argv", ["bunkr-api", "--db-id", "17"]),
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as mock_show,
    ):
        await _run()

    mock_show.assert_awaited_once_with(17)


@pytest.mark.asyncio
async def test_run_route_bare_numeric_positional_is_treated_as_db_id(mock_db):
    """The old `path` positional was dead code (argparse always assigned a
    bare token to `search`, declared first, so `path` could never receive
    it). That's since been fixed by reinterpreting `args.search` itself in
    priority order (JSON path -> numeric ID -> search term), so a bare
    numeric positional like `bunkr-api 17` now correctly jumps straight to
    album 17 instead of being treated as a literal search term.
    """
    with (
        patch("sys.argv", ["bunkr-api", "17"]),
        patch(
            "bunkr_api.cli.run_scrape_interactive", new_callable=AsyncMock, return_value=None
        ) as mock_scrape,
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as mock_show,
    ):
        await _run()

    mock_show.assert_awaited_once_with(17)
    mock_scrape.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_route_search_term(mock_db):
    with (
        patch("sys.argv", ["bunkr-api", "some search"]),
        patch(
            "bunkr_api.cli.run_scrape_interactive", new_callable=AsyncMock, return_value=55
        ) as mock_scrape,
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as mock_show,
    ):
        await _run()

    mock_scrape.assert_awaited_once()
    mock_show.assert_awaited_once_with(55)


@pytest.mark.asyncio
async def test_run_route_top_flag(mock_db):
    with (
        patch("sys.argv", ["bunkr-api", "--top", "albums"]),
        patch(
            "bunkr_api.cli.run_top_engine_interactive", new_callable=AsyncMock, return_value=66
        ) as mock_top,
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as mock_show,
    ):
        await _run()

    mock_top.assert_awaited_once()
    mock_show.assert_awaited_once_with(66)


@pytest.mark.asyncio
async def test_run_route_default_falls_back_to_main_loop(mock_db):
    with (
        patch("sys.argv", ["bunkr-api"]),
        patch("bunkr_api.cli.main_loop", new_callable=AsyncMock) as mock_loop,
    ):
        await _run()

    mock_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_default_route_keyboard_interrupt_exits_cleanly(mock_db):
    with (
        patch("sys.argv", ["bunkr-api"]),
        patch("bunkr_api.cli.main_loop", new_callable=AsyncMock, side_effect=KeyboardInterrupt),
        pytest.raises(SystemExit),
    ):
        await _run()


# ============================================================
# main_loop
# ============================================================


@pytest.mark.asyncio
async def test_main_loop_quit_exits(mock_db):
    mock_db.get_all_albums.return_value = []
    with patch("bunkr_api.cli.Prompt.ask", return_value="q"):
        await main_loop()  # should return, not hang


@pytest.mark.asyncio
async def test_main_loop_numeric_selection_opens_album(mock_db):
    mock_db.get_all_albums.return_value = [{"id": 5, "title": "A", "file_count": 1, "is_staged": 0}]

    with (
        patch("bunkr_api.cli.Prompt.ask", side_effect=["1", "q"]),
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as mock_show,
    ):
        await main_loop()

    mock_show.assert_awaited_once_with(5)


@pytest.mark.asyncio
async def test_main_loop_shows_completed_badge_for_fully_downloaded_album(mock_db):
    mock_db.get_all_albums.return_value = [
        {"id": 1, "title": "Finished", "file_count": 2, "is_staged": 0,
         "total_assets": 2, "completed_assets": 2},
        {"id": 2, "title": "Partial", "file_count": 2, "is_staged": 0,
         "total_assets": 2, "completed_assets": 1},
        {"id": 3, "title": "Empty", "file_count": 0, "is_staged": 0,
         "total_assets": 0, "completed_assets": 0},
    ]

    printed = []
    with (
        patch("bunkr_api.cli.Prompt.ask", return_value="q"),
        patch("bunkr_api.cli.console.print", side_effect=lambda *a, **k: printed.extend(a)),
    ):
        await main_loop()

    lines = [str(p) for p in printed]
    finished_line = next(line for line in lines if "Finished" in line)
    partial_line = next(line for line in lines if "Partial" in line)
    empty_line = next(line for line in lines if "Empty" in line)

    assert "[COMPLETED]" in finished_line
    assert "[COMPLETED]" not in partial_line
    assert "[COMPLETED]" not in empty_line  # 0/0 must not count as complete


@pytest.mark.asyncio
async def test_main_loop_search_shortcut(mock_db):
    mock_db.get_all_albums.return_value = []

    with (
        patch("bunkr_api.cli.Prompt.ask", side_effect=["s", "q"]),
        patch(
            "bunkr_api.cli.run_scrape_interactive", new_callable=AsyncMock, return_value=None
        ) as mock_scrape,
    ):
        await main_loop()

    mock_scrape.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_loop_delete_album(mock_db):
    mock_db.get_all_albums.return_value = [{"id": 5, "title": "A", "file_count": 1, "is_staged": 0}]
    mock_conn = MagicMock()
    mock_db.connection.return_value.__enter__.return_value = mock_conn
    mock_db.connection.return_value.__exit__.return_value = False

    with patch("bunkr_api.cli.Prompt.ask", side_effect=["d", "1", "y", "q"]):
        await main_loop()

    executed_sql = [c.args[0] for c in mock_conn.execute.call_args_list]
    assert any("DELETE FROM assets" in sql for sql in executed_sql)
    assert any("DELETE FROM albums" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_main_loop_invalid_selection_shows_error(mock_db, capsys):
    mock_db.get_all_albums.return_value = []

    with patch("bunkr_api.cli.Prompt.ask", side_effect=["not_a_command", "q"]):
        await main_loop()

    assert "Invalid selection" in capsys.readouterr().out


# ============================================================
# main
# ============================================================


def test_main_wraps_run_via_asyncio_run(mock_db):
    with (
        patch("sys.argv", ["bunkr-api"]),
        patch("bunkr_api.cli.main_loop", new_callable=AsyncMock) as mock_loop,
    ):
        main()

    mock_loop.assert_awaited_once()