import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api.core.tokens import (
    daemon_loop,
    main,
    mint_now,
    mint_single_url_async,
    process_asset_task,
    refresh_all_tokens_async,
)

# ============================================================
# NOTE ON PATCHING TARGETS
# ============================================================
# `execute_request_with_retry_async` and `AsyncSession` are imported at the
# TOP of tokens.py, so they're patched where they're looked up:
# "bunkr_api.core.tokens.<name>".
#
# `DatabaseManager` (inside daemon_loop) is imported LOCALLY
# (`from .db import DatabaseManager`), which re-fetches the name from its
# source module on every call — so it's patched at its source:
# "bunkr_api.core.db.DatabaseManager".
# ============================================================


# ============================================================
# mint_single_url_async
# ============================================================


@pytest.mark.asyncio
async def test_mint_single_url_async_rejects_empty_file_id():
    with pytest.raises(ValueError):
        await mint_single_url_async(AsyncMock(), "")


@pytest.mark.asyncio
async def test_mint_single_url_async_rejects_none_value():
    with pytest.raises(ValueError):
        await mint_single_url_async(AsyncMock(), None)


@pytest.mark.asyncio
async def test_mint_single_url_async_rejects_literal_none_string():
    """A file_id that's the literal string 'None' (e.g. from a bad
    str(None) upstream) is also rejected, not just a real None/empty value.
    """
    with pytest.raises(ValueError):
        await mint_single_url_async(AsyncMock(), "None")


@pytest.mark.asyncio
async def test_mint_single_url_async_success_stitches_final_url():
    session = AsyncMock()
    meta_response = MagicMock()
    meta_response.json.return_value = {
        "mediafiles": "https://cdn.example.com",
        "path": "/files/abc.mp4",
        "original": "abc.mp4",
    }
    sign_response = MagicMock()
    sign_response.json.return_value = {"token": "tok123", "ex": 1893456000}

    with patch(
        "bunkr_api.core.tokens.execute_request_with_retry_async",
        new_callable=AsyncMock,
        side_effect=[meta_response, sign_response],
    ) as mock_fetch:
        url = await mint_single_url_async(session, "999")

    assert url == "https://cdn.example.com/files/abc.mp4?n=abc.mp4&token=tok123&ex=1893456000"
    assert mock_fetch.call_count == 2

    first_call = mock_fetch.call_args_list[0]
    assert first_call.args[1] == "https://dl.bunkr.cr/api/_001_v2"
    assert first_call.kwargs["method"] == "POST"
    assert first_call.kwargs["json_payload"] == {"id": "999"}

    second_call = mock_fetch.call_args_list[1]
    assert "path=" in second_call.args[1]
    assert second_call.kwargs["method"] == "GET"


@pytest.mark.asyncio
async def test_mint_single_url_async_missing_metadata_raises():
    session = AsyncMock()
    meta_response = MagicMock()
    meta_response.json.return_value = {"mediafiles": None, "path": "/x", "original": "y"}

    with (
        patch(
            "bunkr_api.core.tokens.execute_request_with_retry_async",
            new_callable=AsyncMock,
            return_value=meta_response,
        ),
        pytest.raises(ValueError),
    ):
        await mint_single_url_async(session, "999")


# ============================================================
# mint_now
# ============================================================


def test_mint_now_wraps_the_async_minter():
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("bunkr_api.core.tokens.AsyncSession", return_value=mock_session),
        patch(
            "bunkr_api.core.tokens.mint_single_url_async",
            new_callable=AsyncMock,
            return_value="http://final",
        ) as mock_mint,
    ):
        result = mint_now("123")

    assert result == "http://final"
    mock_mint.assert_awaited_once_with(mock_session, "123")


# ============================================================
# process_asset_task
# ============================================================


@pytest.mark.asyncio
async def test_process_asset_task_resolves_true_file_id():
    db = MagicMock()
    sem = asyncio.Semaphore(1)
    progress = MagicMock()
    asset = {"id": 1, "true_file_id": 42}

    with patch(
        "bunkr_api.core.tokens.mint_single_url_async",
        new_callable=AsyncMock,
        return_value="http://fresh",
    ) as mock_mint:
        await process_asset_task(AsyncMock(), db, sem, asset, progress, task_id=0)

    assert mock_mint.call_args[0][1] == "42"
    db.update_asset_url.assert_called_once_with(1, "http://fresh")
    progress.advance.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_process_asset_task_falls_back_to_slug_id():
    db = MagicMock()
    sem = asyncio.Semaphore(1)
    progress = MagicMock()
    asset = {"id": 2, "slug_id": 77}

    with patch(
        "bunkr_api.core.tokens.mint_single_url_async",
        new_callable=AsyncMock,
        return_value="http://x",
    ) as mock_mint:
        await process_asset_task(AsyncMock(), db, sem, asset, progress, task_id=0)

    assert mock_mint.call_args[0][1] == "77"


@pytest.mark.asyncio
async def test_process_asset_task_parses_id_from_source_url_f_pattern():
    db = MagicMock()
    sem = asyncio.Semaphore(1)
    progress = MagicMock()
    asset = {"id": 3, "source_url": "https://bunkr.cr/f/abc123?query=1"}

    with patch(
        "bunkr_api.core.tokens.mint_single_url_async",
        new_callable=AsyncMock,
        return_value="http://x",
    ) as mock_mint:
        await process_asset_task(AsyncMock(), db, sem, asset, progress, task_id=0)

    assert mock_mint.call_args[0][1] == "abc123"


@pytest.mark.asyncio
async def test_process_asset_task_falls_back_to_basename_when_no_f_pattern():
    db = MagicMock()
    sem = asyncio.Semaphore(1)
    progress = MagicMock()
    asset = {"id": 4, "source_url": "https://bunkr.cr/other/path/xyz789"}

    with patch(
        "bunkr_api.core.tokens.mint_single_url_async",
        new_callable=AsyncMock,
        return_value="http://x",
    ) as mock_mint:
        await process_asset_task(AsyncMock(), db, sem, asset, progress, task_id=0)

    assert mock_mint.call_args[0][1] == "xyz789"


@pytest.mark.asyncio
async def test_process_asset_task_no_resolvable_id_skips_but_still_advances():
    db = MagicMock()
    sem = asyncio.Semaphore(1)
    progress = MagicMock()
    asset = {"id": 5}  # no true_file_id, slug_id, or source_url

    with patch("bunkr_api.core.tokens.mint_single_url_async", new_callable=AsyncMock) as mock_mint:
        await process_asset_task(AsyncMock(), db, sem, asset, progress, task_id=0)

    mock_mint.assert_not_awaited()
    db.update_asset_url.assert_not_called()
    progress.advance.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_process_asset_task_swallows_expected_exceptions():
    db = MagicMock()
    sem = asyncio.Semaphore(1)
    progress = MagicMock()
    asset = {"id": 6, "true_file_id": 1}

    with patch(
        "bunkr_api.core.tokens.mint_single_url_async",
        new_callable=AsyncMock,
        side_effect=ValueError("bad"),
    ):
        await process_asset_task(
            AsyncMock(), db, sem, asset, progress, task_id=0
        )  # should not raise

    progress.advance.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_process_asset_task_lets_unexpected_exceptions_propagate():
    """Only TimeoutError, CurlError, sqlite3.Error, KeyError, and ValueError
    are caught — anything else (e.g. a bare RuntimeError) propagates up,
    which asyncio.gather() in refresh_all_tokens_async would then surface
    for the whole batch.
    """
    db = MagicMock()
    sem = asyncio.Semaphore(1)
    progress = MagicMock()
    asset = {"id": 7, "true_file_id": 1}

    with (
        patch(
            "bunkr_api.core.tokens.mint_single_url_async",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError),
    ):
        await process_asset_task(AsyncMock(), db, sem, asset, progress, task_id=0)

    # `finally` still runs even though the exception itself propagates.
    progress.advance.assert_called_once_with(0)


# ============================================================
# refresh_all_tokens_async
# ============================================================


@pytest.mark.asyncio
async def test_refresh_all_tokens_async_processes_every_asset():
    db = MagicMock()
    assets = [{"id": 1, "true_file_id": 1}, {"id": 2, "true_file_id": 2}]

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("bunkr_api.core.tokens.AsyncSession", return_value=mock_session),
        patch("bunkr_api.core.tokens.process_asset_task", new_callable=AsyncMock) as mock_task,
    ):
        await refresh_all_tokens_async(db, assets, max_workers=2)

    assert mock_task.call_count == 2


# ============================================================
# daemon_loop
# ============================================================


def test_daemon_loop_targeted_one_shot_refreshes_and_stops():
    mock_db = MagicMock()
    mock_db.get_config_val.return_value = "4"
    mock_db.get_needs_refresh.return_value = [{"id": 1}]

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch(
            "bunkr_api.core.tokens.refresh_all_tokens_async", new_callable=AsyncMock
        ) as mock_refresh,
    ):
        daemon_loop(album_id=5)

    mock_db.get_needs_refresh.assert_called_once_with(album_id=5)
    mock_refresh.assert_awaited_once()


def test_daemon_loop_targeted_no_expiring_assets_skips_refresh_call():
    mock_db = MagicMock()
    mock_db.get_config_val.return_value = "4"
    mock_db.get_needs_refresh.return_value = []

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch(
            "bunkr_api.core.tokens.refresh_all_tokens_async", new_callable=AsyncMock
        ) as mock_refresh,
    ):
        daemon_loop(album_id=5)

    mock_refresh.assert_not_awaited()


def test_daemon_loop_full_daemon_stops_cleanly_on_keyboard_interrupt():
    mock_db = MagicMock()
    mock_db.get_config_val.return_value = "4"
    mock_db.get_needs_refresh.return_value = []  # nothing to do each pass

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch("bunkr_api.core.tokens.time.sleep", side_effect=KeyboardInterrupt),
    ):
        daemon_loop(album_id=None)  # should exit cleanly, not hang or raise


def test_daemon_loop_full_daemon_recovers_from_unexpected_error_then_stops():
    mock_db = MagicMock()
    mock_db.get_config_val.return_value = "4"
    # First pass: an unexpected error triggers the except-Exception recovery
    # path (10s retry sleep). Second pass: KeyboardInterrupt ends the loop.
    mock_db.get_needs_refresh.side_effect = [RuntimeError("db hiccup"), KeyboardInterrupt]

    with (
        patch("bunkr_api.core.db.DatabaseManager", return_value=mock_db),
        patch("bunkr_api.core.tokens.time.sleep") as mock_sleep,
    ):
        daemon_loop(album_id=None)

    # The error-recovery path always sleeps exactly 10s, regardless of the
    # configured poll interval.
    assert mock_sleep.call_args_list[0].args == (10,)


# ============================================================
# main
# ============================================================


def test_main_forwards_album_id_to_daemon_loop():
    with (
        patch("sys.argv", ["bunkr-mint", "--album-id", "42"]),
        patch("bunkr_api.core.tokens.daemon_loop") as mock_daemon,
    ):
        main()

    mock_daemon.assert_called_once_with(album_id=42)


def test_main_defaults_album_id_to_none():
    with (
        patch("sys.argv", ["bunkr-mint"]),
        patch("bunkr_api.core.tokens.daemon_loop") as mock_daemon,
    ):
        main()

    mock_daemon.assert_called_once_with(album_id=None)
