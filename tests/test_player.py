import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api.media.player import PlayerEngine, prompt_for_inputs, run_player_logic

# ============================================================
# NOTE ON PATCHING TARGETS
# ============================================================
# `AsyncSession` is imported LOCALLY inside resolve_tokens_async
# (`from curl_cffi.requests import AsyncSession`), which re-fetches the name
# from its source module on every call — so it's patched at its source:
# "curl_cffi.requests.AsyncSession".
#
# `mint_single_url_async` and `DatabaseManager` (inside run_player_logic)
# differ: mint_single_url_async is imported at the TOP of player.py, so it's
# patched where it's looked up: "bunkr_api.media.player.mint_single_url_async".
# DatabaseManager is imported LOCALLY inside run_player_logic
# (`from ..core.db import DatabaseManager`), so it's patched at its source:
# "bunkr_api.core.db.DatabaseManager".
# ============================================================


# ============================================================
# resolve_tokens_async
# ============================================================


@pytest.mark.asyncio
async def test_resolve_tokens_async_skips_valid_unexpired_tokens(temp_db):
    engine = PlayerEngine(temp_db)
    now = time.time()
    assets = [
        {
            "id": 1,
            "signed_cdn_url": "http://x",
            "token_expiry_timestamp": now + 3600,
            "true_file_id": 1,
        }
    ]

    with patch("curl_cffi.requests.AsyncSession") as mock_session_cls:
        await engine.resolve_tokens_async(assets)

    mock_session_cls.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_tokens_async_refreshes_missing_and_expiring(temp_db):
    """Missing url, missing expiry, and expiring-within-60s all get
    refreshed; a comfortably-valid token is left alone.
    """
    temp_db.get_config_val = MagicMock(return_value="4")
    temp_db.update_asset_url = MagicMock()
    engine = PlayerEngine(temp_db)
    now = time.time()

    assets = [
        {
            "id": 1,
            "signed_cdn_url": None,
            "token_expiry_timestamp": None,
            "true_file_id": 10,
            "title": "a",
        },
        {
            "id": 2,
            "signed_cdn_url": "http://old",
            "token_expiry_timestamp": now + 10,
            "true_file_id": 20,
            "title": "b",
        },
        {
            "id": 3,
            "signed_cdn_url": "http://valid",
            "token_expiry_timestamp": now + 3600,
            "true_file_id": 30,
            "title": "c",
        },
    ]

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("curl_cffi.requests.AsyncSession", return_value=mock_session),
        patch("bunkr_api.media.player.mint_single_url_async", new_callable=AsyncMock) as mock_mint,
    ):
        mock_mint.side_effect = lambda session, fid: f"http://fresh/{fid}"
        await engine.resolve_tokens_async(assets)

    refreshed_ids = {c.args[0] for c in temp_db.update_asset_url.call_args_list}
    assert refreshed_ids == {1, 2}


@pytest.mark.asyncio
async def test_resolve_tokens_async_true_file_id_falls_back_to_slug_id(temp_db):
    temp_db.get_config_val = MagicMock(return_value="4")
    temp_db.update_asset_url = MagicMock()
    engine = PlayerEngine(temp_db)

    assets = [
        {
            "id": 1,
            "signed_cdn_url": None,
            "token_expiry_timestamp": None,
            "slug_id": 555,
            "title": "a",
        }
    ]

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("curl_cffi.requests.AsyncSession", return_value=mock_session),
        patch("bunkr_api.media.player.mint_single_url_async", new_callable=AsyncMock) as mock_mint,
    ):
        mock_mint.return_value = "http://fresh"
        await engine.resolve_tokens_async(assets)

    mock_mint.assert_awaited_once_with(mock_session, "555")


@pytest.mark.asyncio
async def test_resolve_tokens_async_survives_individual_mint_failures(temp_db):
    temp_db.get_config_val = MagicMock(return_value="4")
    temp_db.update_asset_url = MagicMock()
    engine = PlayerEngine(temp_db)

    assets = [
        {
            "id": 1,
            "signed_cdn_url": None,
            "token_expiry_timestamp": None,
            "true_file_id": 10,
            "title": "a",
        },
        {
            "id": 2,
            "signed_cdn_url": None,
            "token_expiry_timestamp": None,
            "true_file_id": 20,
            "title": "b",
        },
    ]

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def flaky_mint(session, fid):
        if fid == "10":
            raise Exception("mint failed")
        return f"http://fresh/{fid}"

    with (
        patch("curl_cffi.requests.AsyncSession", return_value=mock_session),
        patch("bunkr_api.media.player.mint_single_url_async", side_effect=flaky_mint),
    ):
        await engine.resolve_tokens_async(assets)  # should not raise

    temp_db.update_asset_url.assert_called_once_with(2, "http://fresh/20")


@pytest.mark.asyncio
async def test_resolve_tokens_async_missing_title_no_longer_crashes(temp_db):
    """Previously, the except-block's error log did `a.get('title')[:20]`,
    which raised TypeError if 'title' was missing/None — escaping the
    handler and crashing the whole gather() batch. Now falls back to
    'unknown' instead, so a mint failure for a title-less asset is just
    logged and skipped like any other failure.
    """
    temp_db.get_config_val = MagicMock(return_value="4")
    temp_db.update_asset_url = MagicMock()
    engine = PlayerEngine(temp_db)

    assets = [
        {
            "id": 1,
            "signed_cdn_url": None,
            "token_expiry_timestamp": None,
            "true_file_id": 10,
        },  # no "title"
        {
            "id": 2,
            "signed_cdn_url": None,
            "token_expiry_timestamp": None,
            "true_file_id": 20,
            "title": "b",
        },
    ]

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def flaky_mint(session, fid):
        if fid == "10":
            raise Exception("mint failed")
        return f"http://fresh/{fid}"

    with (
        patch("curl_cffi.requests.AsyncSession", return_value=mock_session),
        patch("bunkr_api.media.player.mint_single_url_async", side_effect=flaky_mint),
    ):
        await engine.resolve_tokens_async(assets)  # should not raise

    temp_db.update_asset_url.assert_called_once_with(2, "http://fresh/20")


# ============================================================
# play_mpv
# ============================================================


@pytest.mark.asyncio
async def test_play_mpv_success_launches_and_cleans_up(temp_db):
    engine = PlayerEngine(temp_db)
    engine.poll_mpv_status = MagicMock()  # avoid a real IPC connection attempt

    mock_proc = MagicMock()
    mock_proc.wait = MagicMock()

    queue = [(1, "Song A", "http://a"), (2, "Song B", "http://b")]

    with patch("bunkr_api.media.player.subprocess.Popen", return_value=mock_proc) as mock_popen:
        await engine.play_mpv(queue)

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert "mpv" in cmd
    playlist_path = cmd[-1]
    assert playlist_path.endswith(".m3u")
    mock_proc.wait.assert_called_once()
    # finally block should have deleted the temp playlist file
    from pathlib import Path

    assert not Path(playlist_path).exists()


@pytest.mark.asyncio
async def test_play_mpv_missing_binary_does_not_raise(temp_db):
    engine = PlayerEngine(temp_db)
    engine.poll_mpv_status = MagicMock()
    queue = [(1, "Song A", "http://a")]

    with patch("bunkr_api.media.player.subprocess.Popen", side_effect=FileNotFoundError()):
        await engine.play_mpv(queue)  # should not raise


# ============================================================
# play_vlc
# ============================================================


@pytest.mark.asyncio
async def test_play_vlc_success(temp_db):
    engine = PlayerEngine(temp_db)
    queue = [(1, "Song A", "http://a")]

    with (
        patch("bunkr_api.media.player.subprocess.run") as mock_run,
        patch("bunkr_api.media.player.shutil.which", return_value="/usr/bin/vlc"),
    ):
        await engine.play_vlc(queue)

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "vlc"


@pytest.mark.asyncio
async def test_play_vlc_windows_fallback_path_when_not_on_path(temp_db):
    engine = PlayerEngine(temp_db)
    queue = [(1, "Song A", "http://a")]

    with (
        patch("bunkr_api.media.player.subprocess.run") as mock_run,
        patch("bunkr_api.media.player.shutil.which", return_value=None),
        patch("bunkr_api.media.player.os.name", "nt"),
    ):
        await engine.play_vlc(queue)

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == r"C:\Program Files\VideoLAN\VLC\vlc.exe"


# ============================================================
# connect_to_ipc
# ============================================================


def test_connect_to_ipc_windows_success(temp_db):
    engine = PlayerEngine(temp_db)
    fake_handle = MagicMock()

    with (
        patch("bunkr_api.media.player.os.name", "nt"),
        patch("builtins.open", return_value=fake_handle) as mock_open,
    ):
        result = engine.connect_to_ipc(r"\\.\pipe\test")

    mock_open.assert_called_once_with(r"\\.\pipe\test", "r+b", buffering=0)
    assert result is fake_handle


def test_connect_to_ipc_windows_failure_wraps_oserror(temp_db):
    engine = PlayerEngine(temp_db)

    with (
        patch("bunkr_api.media.player.os.name", "nt"),
        patch("builtins.open", side_effect=OSError("pipe busy")),
        pytest.raises(ConnectionError),
    ):
        engine.connect_to_ipc(r"\\.\pipe\test")


def test_connect_to_ipc_unix_success(temp_db, tmp_path):
    engine = PlayerEngine(temp_db)
    mock_sock = MagicMock()
    mock_file = MagicMock()
    mock_sock.makefile.return_value = mock_file
    fake_sock_path = str(tmp_path / "fake.sock")

    with (
        patch("bunkr_api.media.player.os.name", "posix"),
        patch("bunkr_api.media.player.socket.socket", return_value=mock_sock),
        patch("bunkr_api.media.player.socket.AF_UNIX", 1, create=True),
    ):
        result = engine.connect_to_ipc(fake_sock_path)

    mock_sock.connect.assert_called_once_with(fake_sock_path)
    mock_sock.makefile.assert_called_once_with("rw", encoding="utf-8")
    assert result is mock_file


# ============================================================
# poll_mpv_status
#
# This function has no return value and stores its parsed state (song
# title, position, cache) only in a local variable feeding a Rich Live
# display — nothing is exposed for direct assertion. So these are
# necessarily closer to smoke tests: confirm it drains a fake IPC stream,
# handles malformed input, and cleans up/gives up correctly, rather than
# asserting on the specific parsed values.
# ============================================================


def test_poll_mpv_status_returns_immediately_if_stop_event_already_set(temp_db):
    engine = PlayerEngine(temp_db)
    stop_event = threading.Event()
    stop_event.set()

    with patch.object(engine, "connect_to_ipc") as mock_connect:
        engine.poll_mpv_status("fake_pipe", stop_event, total_tracks=1)

    mock_connect.assert_not_called()


def test_poll_mpv_status_processes_property_change_events_then_closes(temp_db):
    engine = PlayerEngine(temp_db)
    stop_event = threading.Event()

    events = [
        json.dumps({"event": "property-change", "id": 1, "data": "My Song"}).encode("utf-8")
        + b"\n",
        json.dumps({"event": "property-change", "id": 4, "data": 42.5}).encode("utf-8") + b"\n",
        b"",  # empty read -> breaks the loop
    ]
    fake_sock = MagicMock()
    fake_sock.readline.side_effect = events

    with (
        patch.object(engine, "connect_to_ipc", return_value=fake_sock),
        patch("bunkr_api.media.player.os.name", "nt"),
    ):
        engine.poll_mpv_status("fake_pipe", stop_event, total_tracks=5)  # should not raise

    fake_sock.close.assert_called_once()


def test_poll_mpv_status_skips_malformed_json_lines(temp_db):
    engine = PlayerEngine(temp_db)
    stop_event = threading.Event()

    fake_sock = MagicMock()
    fake_sock.readline.side_effect = [
        b"not valid json\n",
        b"",  # end
    ]

    with (
        patch.object(engine, "connect_to_ipc", return_value=fake_sock),
        patch("bunkr_api.media.player.os.name", "nt"),
    ):
        engine.poll_mpv_status("fake_pipe", stop_event, total_tracks=1)  # should not raise

    fake_sock.close.assert_called_once()


def test_poll_mpv_status_gives_up_after_retries_if_connection_never_succeeds(temp_db):
    """If connect_to_ipc never succeeds, the function retries up to 50
    times (0.1s apart) before giving up and returning cleanly rather than
    hanging forever. time.sleep is patched out so this runs instantly
    instead of taking the real ~5 seconds.
    """
    engine = PlayerEngine(temp_db)
    stop_event = threading.Event()

    with (
        patch.object(engine, "connect_to_ipc", side_effect=Exception("no pipe")),
        patch("bunkr_api.media.player.os.name", "nt"),
        patch("bunkr_api.media.player.time.sleep") as mock_sleep,
    ):
        engine.poll_mpv_status("fake_pipe", stop_event, total_tracks=1)  # should return, not hang

    assert mock_sleep.call_count == 50


# ============================================================
# prompt_for_inputs
# ============================================================


def test_prompt_for_inputs_shows_completed_badge_for_fully_downloaded_album(temp_db):
    finished_id, _, _ = temp_db.register_album_from_json({
        "selected_album": {"title": "Finished Album", "album_index_number": 1},
        "files_found": [{"href": "https://x/1", "title": "a.mp4"}],
    })
    for a in temp_db.get_album_assets(finished_id):
        temp_db.update_download_status(a["id"], "COMPLETED", "/tmp/out.mp4")  # noqa: S108

    partial_id, _, _ = temp_db.register_album_from_json({
        "selected_album": {"title": "Partial Album", "album_index_number": 2},
        "files_found": [
            {"href": "https://y/1", "title": "b.mp4"},
            {"href": "https://y/2", "title": "c.mp4"},
        ],
    })
    partial_assets = temp_db.get_album_assets(partial_id)
    temp_db.update_download_status(partial_assets[0]["id"], "COMPLETED", "/tmp/out2.mp4")  # noqa: S108

    printed = []
    with (
        patch("bunkr_api.media.player.console.print", side_effect=lambda *a, **k: printed.extend(a)),
        patch("bunkr_api.media.player.Prompt.ask", return_value="q"),
        pytest.raises(SystemExit),
    ):
        prompt_for_inputs(temp_db)

    lines = [str(p) for p in printed]
    finished_line = next(line for line in lines if "Finished Album" in line)
    partial_line = next(line for line in lines if "Partial Album" in line)

    assert "[COMPLETED]" in finished_line
    assert "[COMPLETED]" not in partial_line


def test_prompt_for_inputs_quit_exits(temp_db):
    temp_db.get_all_albums = MagicMock(return_value=[])
    with (
        patch("bunkr_api.media.player.Prompt.ask", return_value="q"),
        pytest.raises(SystemExit),
    ):
        prompt_for_inputs(temp_db)


def test_prompt_for_inputs_staged_keyword(temp_db):
    temp_db.get_all_albums = MagicMock(return_value=[])
    with patch("bunkr_api.media.player.Prompt.ask", side_effect=["staged", "vlc"]):
        result = prompt_for_inputs(temp_db)

    assert result == (None, None, "all", "vlc", True)


def test_prompt_for_inputs_numeric_selection_maps_to_catalog_album(temp_db):
    album_row = {"id": 42, "title": "Cool Album", "file_count": 10, "is_staged": 0}
    temp_db.get_all_albums = MagicMock(return_value=[album_row])
    with patch("bunkr_api.media.player.Prompt.ask", side_effect=["1", "all", "mpv"]):
        result = prompt_for_inputs(temp_db)

    input_path, db_id, selection, player, run_staged = result
    assert db_id == 42
    assert input_path is None
    assert run_staged is False


def test_prompt_for_inputs_numeric_out_of_catalog_range_treated_as_raw_db_id(temp_db):
    temp_db.get_all_albums = MagicMock(return_value=[])
    with patch("bunkr_api.media.player.Prompt.ask", side_effect=["999", "all", "mpv"]):
        result = prompt_for_inputs(temp_db)

    assert result[1] == 999  # raw db_id, not a catalog index


def test_prompt_for_inputs_unrecognized_input_exits(temp_db):
    temp_db.get_all_albums = MagicMock(return_value=[])
    with (
        patch("bunkr_api.media.player.Prompt.ask", return_value="not_a_real_path_or_number"),
        pytest.raises(SystemExit),
    ):
        prompt_for_inputs(temp_db)


def test_prompt_for_inputs_valid_json_path(temp_db, tmp_path):
    fake_json = tmp_path / "album.json"
    fake_json.write_text("{}")
    temp_db.get_all_albums = MagicMock(return_value=[])
    with patch("bunkr_api.media.player.Prompt.ask", side_effect=[str(fake_json), "all", "mpv"]):
        result = prompt_for_inputs(temp_db)

    input_path, db_id, selection, player, run_staged = result
    assert input_path == fake_json
    assert db_id is None


# ============================================================
# run_player_logic
# ============================================================


@pytest.mark.asyncio
async def test_run_player_logic_staged_flow_excludes_completed_album_staged_asset(temp_db):
    """Regression test: db.get_staged_assets() (not raw duplicated SQL) is
    used here, so a completed asset whose *album* is still flagged staged
    must not resurface in the --staged playback queue.
    """
    album_id, _, _ = temp_db.register_album_from_json({
        "selected_album": {"title": "Two File Album", "album_index_number": 1},
        "files_found": [
            {"href": "https://link.com/f/a", "title": "a.mp4"},
            {"href": "https://link.com/f/b", "title": "b.mp4"},
        ],
    })
    assets = temp_db.get_album_assets(album_id)
    asset_a_id, _asset_b_id = assets[0]["id"], assets[1]["id"]

    # Mirrors inspector.py's toggle_staging(target="album"): both the album
    # row and every asset in it get is_staged=1.
    with temp_db.connection() as conn:
        conn.execute("UPDATE albums SET is_staged = 1 WHERE id = ?", (album_id,))
        conn.execute("UPDATE assets SET is_staged = 1 WHERE album_id = ?", (album_id,))

    # Asset A finishes downloading; only its own flag clears (album flag
    # stays 1, matching real downloader.py behavior before album-sync runs).
    with temp_db.connection() as conn:
        conn.execute("UPDATE assets SET is_staged = 0 WHERE id = ?", (asset_a_id,))
    temp_db.update_download_status(asset_a_id, "COMPLETED", "/tmp/a.mp4")  # noqa: S108
    temp_db.get_valid_url = MagicMock(side_effect=lambda aid: f"https://resolved/{aid}")

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=temp_db),
        patch(
            "bunkr_api.media.player.PlayerEngine.resolve_tokens_async", new_callable=AsyncMock
        ),
        patch("bunkr_api.media.player.PlayerEngine.play_mpv", new_callable=AsyncMock) as mock_play,
    ):
        await run_player_logic(args_staged=True)

    mock_play.assert_awaited_once()
    queue = mock_play.call_args[0][0]
    titles = [title for _idx, title, _url in queue]
    assert titles == ["b.mp4"], (
        "the completed asset (a.mp4) resurfaced via the stale album-level staged flag"
    )


@pytest.mark.asyncio
async def test_run_player_logic_staged_flow(temp_db):
    mock_db = MagicMock()
    fake_row = {
        "id": 1,
        "title": "Song",
        "original_filename": "song.mp4",
        "signed_cdn_url": "http://x",
        "token_expiry_timestamp": None,
        "true_file_id": 99,
    }
    mock_db.get_staged_assets.return_value = [fake_row]
    mock_db.get_valid_url.return_value = "http://resolved"

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch(
            "bunkr_api.media.player.PlayerEngine.resolve_tokens_async", new_callable=AsyncMock
        ) as mock_resolve,
        patch("bunkr_api.media.player.PlayerEngine.play_mpv", new_callable=AsyncMock) as mock_play,
    ):
        await run_player_logic(args_staged=True)

    mock_resolve.assert_awaited_once()
    mock_play.assert_awaited_once()
    queue_arg = mock_play.call_args[0][0]
    assert queue_arg == [(1, "Song", "http://resolved")]


@pytest.mark.asyncio
async def test_run_player_logic_no_assets_returns_early(temp_db):
    mock_db = MagicMock()
    mock_db.get_album_assets.return_value = []

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch("bunkr_api.media.player.PlayerEngine.play_mpv", new_callable=AsyncMock) as mock_play,
    ):
        await run_player_logic(args_db_id=5)

    mock_play.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_player_logic_json_path_skips_token_resolution(temp_db, tmp_path):
    """Assets loaded from a legacy JSON file have id=None, so they're
    filtered out of db_backed_assets and resolve_tokens_async should never
    be called for a pure-JSON run.
    """
    mock_db = MagicMock()
    json_file = tmp_path / "album.json"
    json_file.write_text(
        json.dumps(
            {"files_found": [{"title": "Song", "signed_cdn_url": "http://x", "true_file_id": 1}]}
        )
    )

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch(
            "bunkr_api.media.player.PlayerEngine.resolve_tokens_async", new_callable=AsyncMock
        ) as mock_resolve,
        patch("bunkr_api.media.player.PlayerEngine.play_mpv", new_callable=AsyncMock) as mock_play,
        patch(
            "bunkr_api.media.player.prompt_for_inputs",
            return_value=(json_file, None, "all", "mpv", False),
        ),
    ):
        await run_player_logic()

    mock_resolve.assert_not_awaited()
    mock_play.assert_awaited_once()
    queue_arg = mock_play.call_args[0][0]
    assert queue_arg == [(1, "Song", "http://x")]


@pytest.mark.asyncio
async def test_run_player_logic_uses_vlc_when_requested(temp_db):
    mock_db = MagicMock()
    mock_db.get_album_assets.return_value = [
        {
            "id": 1,
            "title": "Song",
            "original_filename": "song.mp4",
            "signed_cdn_url": "http://x",
            "token_expiry_timestamp": None,
            "true_file_id": 1,
        }
    ]
    mock_db.get_valid_url.return_value = "http://resolved"

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch("bunkr_api.media.player.PlayerEngine.resolve_tokens_async", new_callable=AsyncMock),
        patch("bunkr_api.media.player.PlayerEngine.play_vlc", new_callable=AsyncMock) as mock_vlc,
        patch("bunkr_api.media.player.PlayerEngine.play_mpv", new_callable=AsyncMock) as mock_mpv,
    ):
        await run_player_logic(args_db_id=1, args_player="vlc")

    mock_vlc.assert_awaited_once()
    mock_mpv.assert_not_awaited()