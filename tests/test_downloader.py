import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api.media.downloader import (
    DownloadEngine,
    main,
    prompt_for_inputs,
    prompt_for_workers,
)

# ============================================================
# execute_ytdlp_task (sync — unchanged by the async refactor)
# ============================================================

def test_ytdlp_task_success(temp_db, tmp_path):
    """Test successful execution of a yt-dlp task."""
    engine = DownloadEngine(temp_db)

    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)

    asset_data = {
        "title": "sample_video.mp4",
        "db_asset_id": 1,
        "signed_cdn_url": "https://cdn.example.com/file.mp4",
        "album_id": "101",
        "album_title": "My Album",
    }

    temp_db.get_valid_url = MagicMock(return_value="https://cdn.example.com/file.mp4")
    temp_db.update_download_status = MagicMock()

    mock_proc = MagicMock()
    mock_proc.stdout = ["PROGRESS 500 1000\n"]
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0

    # get_album_folder_name produces "#ID_Title" with underscores.
    expected_folder = "#101_My_Album"
    expected_filename = "sample_video.mp4"
    expected_path = tmp_path / expected_folder / expected_filename

    with patch("subprocess.Popen", return_value=mock_proc):
        success = engine.execute_ytdlp_task(
            index=1,
            total_files=1,
            asset_data=asset_data,
            slot_id=0,
            task_id=task_id,
            progress=mock_progress,
            output_root=tmp_path,
        )

    assert success is True
    temp_db.update_download_status.assert_any_call(1, "DOWNLOADING")
    temp_db.update_download_status.assert_any_call(
        1, "COMPLETED", str(expected_path)
    )


def test_ytdlp_task_no_url(temp_db, tmp_path):
    """Test engine handling when no valid CDN URL can be resolved."""
    engine = DownloadEngine(temp_db)

    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)

    asset_data = {
        "title": "missing.mp4",
        "db_asset_id": 99,
        "signed_cdn_url": None,
    }

    temp_db.get_valid_url = MagicMock(return_value=None)
    temp_db.update_download_status = MagicMock()

    success = engine.execute_ytdlp_task(
        index=1,
        total_files=1,
        asset_data=asset_data,
        slot_id=0,
        task_id=task_id,
        progress=mock_progress,
        output_root=tmp_path,
    )

    assert success is False
    temp_db.update_download_status.assert_called_with(99, "FAILED", error="No URL")


def test_ytdlp_task_shutdown_mid_download_marks_pending(temp_db, tmp_path):
    """If shutdown_event is set while the subprocess is (or was) running,
    the asset should be reset to PENDING rather than COMPLETED/FAILED, so a
    later run can retry it instead of treating it as done or broken.

    subprocess.Popen IS mocked here (unlike the previous version of this
    test) so we're actually exercising the shutdown branch in execute_ytdlp_task,
    not just accidentally catching a FileNotFoundError from a missing
    real yt-dlp binary.
    """
    engine = DownloadEngine(temp_db)
    engine.shutdown_event.set()

    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "test.mp4", "db_asset_id": 1, "signed_cdn_url": "http://example.com"}

    temp_db.get_valid_url = MagicMock(return_value="http://example.com")
    temp_db.update_download_status = MagicMock()

    mock_proc = MagicMock()
    mock_proc.stdout = ["PROGRESS 500 1000\n"]
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
        success = engine.execute_ytdlp_task(
            index=1,
            total_files=1,
            asset_data=asset,
            slot_id=0,
            task_id=task_id,
            progress=mock_progress,
            output_root=tmp_path,
        )

    assert success is False
    temp_db.update_download_status.assert_any_call(1, "PENDING")


@pytest.mark.asyncio
async def test_resolve_tokens_graceful_failure(temp_db):
    """Test that resolve_tokens_async doesn't raise when token minting fails."""
    engine = DownloadEngine(temp_db)

    assets = [{"db_asset_id": 1, "true_file_id": "abc", "signed_cdn_url": None}]

    with patch(
        "bunkr_api.media.downloader.mint_single_url_async",
        side_effect=Exception("API Down"),
    ):
        await engine.resolve_tokens_async(assets)


# ============================================================
# run() — now fully async, using loop.run_in_executor() instead of
# blocking concurrent.futures.wait(). These tests target that scheduling
# logic directly by mocking out execute_ytdlp_task and resolve_tokens_async,
# since the actual yt-dlp subprocess behavior is already covered above.
# ============================================================

def _make_engine_with_mocks(temp_db, execute_side_effect=None):
    engine = DownloadEngine(temp_db)
    engine.resolve_tokens_async = AsyncMock()
    engine.execute_ytdlp_task = MagicMock(
        side_effect=execute_side_effect if execute_side_effect else (lambda *a, **kw: True)
    )
    return engine


@pytest.mark.asyncio
async def test_run_aborts_if_ytdlp_missing(temp_db, tmp_path):
    engine = _make_engine_with_mocks(temp_db)

    with patch("bunkr_api.media.downloader.shutil.which", return_value=None):
        await engine.run([{"title": "x"}], workers=1, output_dir=tmp_path)

    engine.execute_ytdlp_task.assert_not_called()
    engine.resolve_tokens_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_awaits_token_resolution_before_downloading(temp_db, tmp_path):
    engine = _make_engine_with_mocks(temp_db)
    files = [{"title": "a.mp4"}]

    with patch("bunkr_api.media.downloader.shutil.which", return_value="/usr/bin/yt-dlp"):
        await engine.run(files, workers=1, output_dir=tmp_path)

    engine.resolve_tokens_async.assert_awaited_once_with(files)


@pytest.mark.asyncio
async def test_run_processes_all_files_with_correct_one_based_indices(temp_db, tmp_path):
    """Each file should get its real 1-based position as `index`, not a
    hardcoded 0 — otherwise every untitled asset falls back to the same
    'track_0' filename.
    """
    calls = []

    def fake_execute(index, total_files, asset_data, slot_id, task_id, progress, output_root):
        calls.append((index, total_files, asset_data["title"]))
        return True

    engine = _make_engine_with_mocks(temp_db, execute_side_effect=fake_execute)
    files = [{"title": f"file{i}"} for i in range(1, 4)]

    with patch("bunkr_api.media.downloader.shutil.which", return_value="/usr/bin/yt-dlp"):
        await engine.run(files, workers=2, output_dir=tmp_path)

    assert len(calls) == 3
    assert sorted(c[0] for c in calls) == [1, 2, 3]
    for _, total_files, _ in calls:
        assert total_files == 3


@pytest.mark.asyncio
async def test_run_respects_worker_concurrency_limit(temp_db, tmp_path):
    """The core reason for the run_in_executor rewrite: confirm no more than
    `workers` tasks are ever in flight simultaneously.
    """
    lock = threading.Lock()
    state = {"concurrent": 0, "max_concurrent": 0}

    def fake_execute(index, total_files, asset_data, slot_id, task_id, progress, output_root):
        with lock:
            state["concurrent"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
        time.sleep(0.05)
        with lock:
            state["concurrent"] -= 1
        return True

    engine = _make_engine_with_mocks(temp_db, execute_side_effect=fake_execute)
    files = [{"title": f"f{i}"} for i in range(6)]

    with patch("bunkr_api.media.downloader.shutil.which", return_value="/usr/bin/yt-dlp"):
        await engine.run(files, workers=2, output_dir=tmp_path)

    assert state["max_concurrent"] <= 2


@pytest.mark.asyncio
async def test_run_does_not_block_event_loop(temp_db, tmp_path):
    """The actual regression test for the original bug: while run() is
    awaiting worker completion, the event loop must still be free to run
    other coroutines concurrently (e.g. via asyncio.gather). A ticker
    coroutine should keep incrementing while downloads are "in progress".
    """
    def slow_execute(index, total_files, asset_data, slot_id, task_id, progress, output_root):
        time.sleep(0.3)
        return True

    engine = _make_engine_with_mocks(temp_db, execute_side_effect=slow_execute)
    files = [{"title": "a.mp4"}]

    tick_count = 0

    async def ticker():
        nonlocal tick_count
        for _ in range(10):
            await asyncio.sleep(0.03)
            tick_count += 1

    with patch("bunkr_api.media.downloader.shutil.which", return_value="/usr/bin/yt-dlp"):
        await asyncio.gather(
            engine.run(files, workers=1, output_dir=tmp_path),
            ticker(),
        )

    # If run() were blocking the loop the whole time, the ticker would
    # never have gotten a chance to advance.
    assert tick_count > 0


@pytest.mark.asyncio
async def test_run_survives_individual_worker_exceptions(temp_db, tmp_path):
    """A single worker raising shouldn't crash the whole run() call —
    other files should still be processed and run() should return normally.
    """
    def flaky_execute(index, total_files, asset_data, slot_id, task_id, progress, output_root):
        if index == 1:
            raise RuntimeError("simulated worker crash")
        return True

    engine = _make_engine_with_mocks(temp_db, execute_side_effect=flaky_execute)
    files = [{"title": "a.mp4"}, {"title": "b.mp4"}]

    with patch("bunkr_api.media.downloader.shutil.which", return_value="/usr/bin/yt-dlp"):
        # Should not raise despite the index==1 worker's exception.
        await engine.run(files, workers=2, output_dir=tmp_path)

    assert engine.execute_ytdlp_task.call_count == 2

# ============================================================
# execute_ytdlp_task — remaining branches (token errors, non-zero
# exit codes, Popen exceptions, legacy no-db_id items, is_staged reset)
# ============================================================


def test_ytdlp_task_token_lookup_exception_marks_no_url(temp_db, tmp_path):
    """If db.get_valid_url() itself raises, that must be caught and treated
    as 'no URL' rather than propagating and killing the worker.
    """
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "x.mp4", "db_asset_id": 5}

    temp_db.get_valid_url = MagicMock(side_effect=RuntimeError("token service down"))
    temp_db.update_download_status = MagicMock()

    success = engine.execute_ytdlp_task(
        index=1, total_files=1, asset_data=asset, slot_id=0,
        task_id=task_id, progress=mock_progress, output_root=tmp_path,
    )

    assert success is False
    temp_db.update_download_status.assert_called_with(5, "FAILED", error="No URL")
    mock_progress.console.print.assert_any_call(
        "[red][-][/red] Token Error for x.mp4: token service down"
    )


def test_ytdlp_task_nonzero_exit_code_marks_failed(temp_db, tmp_path):
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "x.mp4", "db_asset_id": 3, "signed_cdn_url": "https://cdn/x"}

    temp_db.get_valid_url = MagicMock(return_value="https://cdn/x")
    temp_db.update_download_status = MagicMock()

    mock_proc = MagicMock()
    mock_proc.stdout = []
    mock_proc.wait.return_value = None
    mock_proc.returncode = 1

    with patch("subprocess.Popen", return_value=mock_proc):
        success = engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    assert success is False
    temp_db.update_download_status.assert_any_call(3, "FAILED", error="Exit code 1")


def test_ytdlp_task_popen_exception_marks_failed(temp_db, tmp_path):
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "x.mp4", "db_asset_id": 4, "signed_cdn_url": "https://cdn/x"}

    temp_db.get_valid_url = MagicMock(return_value="https://cdn/x")
    temp_db.update_download_status = MagicMock()

    with patch("subprocess.Popen", side_effect=OSError("yt-dlp binary missing")):
        success = engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    assert success is False
    temp_db.update_download_status.assert_any_call(4, "FAILED", error="yt-dlp binary missing")
    # Slot must always be released even on a hard failure.
    assert 0 not in engine.active_processes


def test_ytdlp_task_popen_exception_during_shutdown_skips_status_update(temp_db, tmp_path):
    """If the process blows up while a shutdown is already in progress, the
    failure shouldn't overwrite whatever terminal status the shutdown path
    is trying to set (e.g. PENDING) with a FAILED status.
    """
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "x.mp4", "db_asset_id": 6, "signed_cdn_url": "https://cdn/x"}

    temp_db.get_valid_url = MagicMock(return_value="https://cdn/x")
    temp_db.update_download_status = MagicMock()
    engine.shutdown_event.set()

    with patch("subprocess.Popen", side_effect=OSError("boom")):
        success = engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    assert success is False
    failed_calls = [c for c in temp_db.update_download_status.call_args_list if c.args[1] == "FAILED"]
    assert failed_calls == []


def test_ytdlp_task_without_db_id_never_touches_database(temp_db, tmp_path):
    """Legacy JSON-import items have no db_asset_id — every DB write in
    execute_ytdlp_task is gated on `if db_id`, so none of them should fire.
    """
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "legacy.mp4", "signed_cdn_url": "https://cdn/legacy"}  # no db_asset_id

    temp_db.get_valid_url = MagicMock()
    temp_db.update_download_status = MagicMock()

    mock_proc = MagicMock()
    mock_proc.stdout = []
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
        success = engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    assert success is True
    temp_db.get_valid_url.assert_not_called()  # falls back to signed_cdn_url directly
    temp_db.update_download_status.assert_not_called()


def test_ytdlp_task_success_resets_is_staged_flag(temp_db, tmp_path):
    """On COMPLETED, the asset's is_staged flag should flip back to 0 so it
    drops out of the staged-download queue.
    """
    engine = DownloadEngine(temp_db)
    reg = temp_db.register_album_from_json({
        "selected_album": {"title": "Staged Album", "album_index_number": 1},
        "files_found": [{"href": "https://link.com/f/staged", "title": "s.mp4"}],
    })
    album_id = reg[0]
    asset_id = temp_db.get_album_assets(album_id)[0]["id"]

    with temp_db.connection() as conn:
        conn.execute("UPDATE assets SET is_staged = 1 WHERE id = ?", (asset_id,))

    temp_db.get_valid_url = MagicMock(return_value="https://cdn/s")

    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "s.mp4", "db_asset_id": asset_id, "signed_cdn_url": "https://cdn/s"}

    mock_proc = MagicMock()
    mock_proc.stdout = []
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc):
        success = engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    assert success is True
    with temp_db.connection() as conn:
        row = conn.execute("SELECT is_staged FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["is_staged"] == 0


# ============================================================
# resolve_tokens_async — remaining branches (early-return when nothing
# actually needs minting, and the successful mint-and-persist path)
# ============================================================


@pytest.mark.asyncio
async def test_resolve_tokens_async_skips_network_when_nothing_needs_refresh(temp_db):
    """All assets already have a far-future token — no session should even
    be opened.
    """
    engine = DownloadEngine(temp_db)
    now = time.time()
    assets = [
        {"db_asset_id": 1, "signed_cdn_url": "https://cdn/x", "token_expiry_timestamp": now + 99999}
    ]

    with patch("curl_cffi.requests.AsyncSession") as mock_session_cls:
        await engine.resolve_tokens_async(assets)

    mock_session_cls.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_tokens_async_skips_network_when_needed_items_have_no_db_id(temp_db):
    """Items needing a refresh but with no db_asset_id (e.g. legacy JSON
    items never registered in the DB) can't be persisted anyway — no
    session should be opened for them either.
    """
    engine = DownloadEngine(temp_db)
    assets = [{"signed_cdn_url": None, "true_file_id": "abc"}]  # no db_asset_id

    with patch("curl_cffi.requests.AsyncSession") as mock_session_cls:
        await engine.resolve_tokens_async(assets)

    mock_session_cls.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_tokens_async_mints_and_persists_fresh_url(temp_db):
    engine = DownloadEngine(temp_db)
    assets = [{"db_asset_id": 1, "true_file_id": "abc", "signed_cdn_url": None}]
    temp_db.update_asset_url = MagicMock()

    mock_session = AsyncMock()
    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("curl_cffi.requests.AsyncSession", return_value=mock_session_cm),
        patch(
            "bunkr_api.media.downloader.mint_single_url_async",
            new_callable=AsyncMock,
            return_value="https://cdn.fresh/abc",
        ),
    ):
        await engine.resolve_tokens_async(assets)

    temp_db.update_asset_url.assert_called_once_with(1, "https://cdn.fresh/abc")


# ============================================================
# run() — interrupt / cancellation handling (the CancelledError-based
# Ctrl+C path). This is the trickiest part of the whole module per the
# inline comments, and previously had zero coverage.
# ============================================================


@pytest.mark.asyncio
async def test_run_handles_cancellation_terminates_processes_without_raising(temp_db, tmp_path):
    """A single Ctrl+C surfaces as CancelledError inside asyncio.wait() on
    modern Python. run() must catch it, set shutdown_event, terminate any
    in-flight subprocess, and return normally instead of propagating the
    cancellation or hanging.
    """
    engine = _make_engine_with_mocks(temp_db)
    fake_proc = MagicMock()
    files = [{"title": "a.mp4"}]

    async def raise_cancelled(*args, **kwargs):
        # Simulate a download actually in flight when the interrupt lands.
        engine.active_processes[0] = fake_proc
        raise asyncio.CancelledError()

    with (
        patch("bunkr_api.media.downloader.shutil.which", return_value="/usr/bin/yt-dlp"),
        patch("bunkr_api.media.downloader.asyncio.wait", new_callable=AsyncMock, side_effect=raise_cancelled),
        patch("bunkr_api.media.downloader.asyncio.sleep", new_callable=AsyncMock),
    ):
        # Must not raise/propagate the cancellation.
        await engine.run(files, workers=1, output_dir=tmp_path)

    assert engine.shutdown_event.is_set()
    fake_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_run_handles_keyboard_interrupt_directly(temp_db, tmp_path):
    """Covers the (rarer, second-Ctrl+C) case where a raw KeyboardInterrupt
    lands directly rather than a CancelledError — same except clause,
    same required cleanup behavior.
    """
    engine = _make_engine_with_mocks(temp_db)
    files = [{"title": "a.mp4"}]

    with (
        patch("bunkr_api.media.downloader.shutil.which", return_value="/usr/bin/yt-dlp"),
        patch("bunkr_api.media.downloader.asyncio.wait", new_callable=AsyncMock, side_effect=KeyboardInterrupt()),
        patch("bunkr_api.media.downloader.asyncio.sleep", new_callable=AsyncMock),
    ):
        await engine.run(files, workers=1, output_dir=tmp_path)

    assert engine.shutdown_event.is_set()


# ============================================================
# prompt_for_workers
# ============================================================


def test_prompt_for_workers_valid_input():
    with patch("bunkr_api.media.downloader.Prompt.ask", return_value="3"):
        assert prompt_for_workers() == 3


def test_prompt_for_workers_clamps_to_max_five():
    with patch("bunkr_api.media.downloader.Prompt.ask", return_value="99"):
        assert prompt_for_workers() == 5


def test_prompt_for_workers_clamps_to_min_one():
    with patch("bunkr_api.media.downloader.Prompt.ask", return_value="0"):
        assert prompt_for_workers() == 1


def test_prompt_for_workers_invalid_input_defaults_to_one():
    with patch("bunkr_api.media.downloader.Prompt.ask", return_value="not a number"):
        assert prompt_for_workers() == 1


# ============================================================
# prompt_for_inputs
# ============================================================


def test_prompt_for_inputs_quit_exits(temp_db):
    with (
        patch("bunkr_api.media.downloader.Prompt.ask", return_value="q"),
        pytest.raises(SystemExit),
    ):
        prompt_for_inputs(temp_db)


def test_prompt_for_inputs_keyboard_interrupt_exits(temp_db):
    with (
        patch("bunkr_api.media.downloader.Prompt.ask", side_effect=KeyboardInterrupt()),
        pytest.raises(SystemExit),
    ):
        prompt_for_inputs(temp_db)


def test_prompt_for_inputs_staged_keyword_returns_staged_flags(temp_db):
    with patch("bunkr_api.media.downloader.Prompt.ask", return_value="staged"):
        result = prompt_for_inputs(temp_db)

    input_path, db_id, selection, workers, staged, triage = result
    assert (input_path, db_id, selection) == (None, None, None)
    assert staged is True
    assert triage is False


def test_prompt_for_inputs_triage_keyword_returns_triage_flags(temp_db):
    with patch("bunkr_api.media.downloader.Prompt.ask", return_value="triage"):
        result = prompt_for_inputs(temp_db)

    input_path, db_id, selection, workers, staged, triage = result
    assert (input_path, db_id, selection) == (None, None, None)
    assert staged is False
    assert triage is True


def test_prompt_for_inputs_numeric_selection_resolves_cataloged_album(temp_db):
    temp_db.register_album_from_json({
        "selected_album": {"title": "Test Album", "album_index_number": 1},
        "files_found": [],
    })

    with patch("bunkr_api.media.downloader.Prompt.ask", side_effect=["1", "5", "2"]):
        input_path, db_id, selection, workers, staged, triage = prompt_for_inputs(temp_db)

    assert db_id == 1
    assert selection == "5"
    assert workers == 2
    assert input_path is None
    assert (staged, triage) == (False, False)


def test_prompt_for_inputs_number_out_of_catalog_range_treated_as_raw_db_id(temp_db):
    """With no matching catalog entry, a bare number is treated as a
    literal database id (e.g. someone typing a --db-id value directly).
    """
    with patch("bunkr_api.media.downloader.Prompt.ask", side_effect=["42", "", "1"]):
        input_path, db_id, selection, workers, staged, triage = prompt_for_inputs(temp_db)

    assert db_id == 42
    assert selection == "all"  # blank selection defaults to 'all'


def test_prompt_for_inputs_valid_file_path(temp_db, tmp_path):
    json_file = tmp_path / "album.json"
    json_file.write_text("{}")

    with patch("bunkr_api.media.downloader.Prompt.ask", side_effect=[str(json_file), "all", "1"]):
        input_path, db_id, selection, workers, staged, triage = prompt_for_inputs(temp_db)

    assert input_path == json_file
    assert db_id is None


def test_prompt_for_inputs_unrecognized_input_exits(temp_db):
    with (
        patch("bunkr_api.media.downloader.Prompt.ask", return_value="not_a_number_or_real_path"),
        pytest.raises(SystemExit),
    ):
        prompt_for_inputs(temp_db)


def test_prompt_for_inputs_db_query_failure_warns_but_continues(temp_db):
    """A broken catalog query shouldn't crash the whole prompt flow — it
    should warn and fall through to an empty catalog.
    """
    temp_db.get_all_albums = MagicMock(side_effect=RuntimeError("db locked"))

    with (
        patch("bunkr_api.media.downloader.Prompt.ask", return_value="q"),
        pytest.raises(SystemExit),
    ):
        prompt_for_inputs(temp_db)


# ============================================================
# main() — the 'bunkr-download' entry point
# ============================================================


def _mock_engine(mock_engine_cls):
    mock_engine = mock_engine_cls.return_value
    mock_engine.run = AsyncMock()
    return mock_engine


def test_main_staged_mode_queries_and_runs():
    with (
        patch("sys.argv", ["bunkr-download", "--staged"]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.get_staged_assets.return_value = [
            {"id": 1, "album_id": 5, "title": "s.mp4", "is_staged": 1}
        ]
        mock_engine = _mock_engine(mock_engine_cls)

        main()

    mock_engine.run.assert_awaited_once()
    files_list = mock_engine.run.call_args[0][0]
    assert len(files_list) == 1
    assert files_list[0]["db_asset_id"] == 1


def test_main_triage_mode_queries_failed_and_runs():
    with (
        patch("sys.argv", ["bunkr-download", "--triage"]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.get_failed_assets.return_value = [
            {"id": 2, "album_id": 9, "title": "failed.mp4", "download_status": "FAILED"}
        ]
        mock_engine = _mock_engine(mock_engine_cls)

        main()

    mock_engine.run.assert_awaited_once()
    assert mock_engine.run.call_args[0][0][0]["db_asset_id"] == 2


def test_main_db_id_album_not_found_returns_without_running():
    with (
        patch("sys.argv", ["bunkr-download", "--db-id", "999"]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_db.connection.return_value.__enter__.return_value = mock_conn
        mock_db.connection.return_value.__exit__.return_value = False
        mock_engine = _mock_engine(mock_engine_cls)

        main()

    mock_engine.run.assert_not_awaited()


def test_main_db_id_found_builds_files_from_album_assets():
    with (
        patch("sys.argv", ["bunkr-download", "--db-id", "7"]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {"id": 7, "title": "Album Seven"}
        mock_db.connection.return_value.__enter__.return_value = mock_conn
        mock_db.connection.return_value.__exit__.return_value = False
        mock_db.get_album_assets.return_value = [{"id": 55, "title": "seven.mp4"}]
        mock_engine = _mock_engine(mock_engine_cls)

        main()

    mock_engine.run.assert_awaited_once()
    files_list = mock_engine.run.call_args[0][0]
    assert files_list[0]["db_asset_id"] == 55
    assert files_list[0]["album_title"] == "Album Seven"


def test_main_input_json_path_builds_files_from_json(tmp_path):
    json_file = tmp_path / "legacy.json"
    json_file.write_text(json.dumps({
        "selected_album": {"title": "Legacy Album", "album_index_number": 3},
        "files_found": [{"title": "l.mp4", "signed_cdn_url": "https://cdn/l", "true_file_id": 1}],
    }))

    with (
        patch("sys.argv", ["bunkr-download", "-i", str(json_file)]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
    ):
        mock_db_cls.return_value
        mock_engine = _mock_engine(mock_engine_cls)

        main()

    mock_engine.run.assert_awaited_once()
    files_list = mock_engine.run.call_args[0][0]
    assert files_list[0]["album_title"] == "Legacy Album"
    assert files_list[0]["db_asset_id"] is None


def test_main_no_files_found_returns_without_running():
    with (
        patch("sys.argv", ["bunkr-download", "--staged"]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.get_staged_assets.return_value = []
        mock_engine = _mock_engine(mock_engine_cls)

        main()

    mock_engine.run.assert_not_awaited()


def test_main_invalid_selection_shows_error_and_returns():
    with (
        patch("sys.argv", ["bunkr-download", "--staged", "-n", "not-a-valid-range"]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.get_staged_assets.return_value = [
            {"id": 1, "album_id": 5, "title": "s.mp4"}
        ]
        mock_engine = _mock_engine(mock_engine_cls)

        main()

    mock_engine.run.assert_not_awaited()


def test_main_keyboard_interrupt_before_run_exits_cleanly():
    """Safety net for an interrupt landing before run()'s own internal
    handler is active yet (e.g. during the pre-flight token minting await).
    """
    with (
        patch("sys.argv", ["bunkr-download", "--staged"]),
        patch("bunkr_api.core.db.DatabaseManager") as mock_db_cls,
        patch("bunkr_api.media.downloader.DownloadEngine") as mock_engine_cls,
        patch("bunkr_api.media.downloader.asyncio.run", side_effect=KeyboardInterrupt()),
    ):
        mock_db = mock_db_cls.return_value
        mock_db.get_staged_assets.return_value = [
            {"id": 1, "album_id": 5, "title": "s.mp4"}
        ]
        # Plain MagicMock, not AsyncMock: asyncio.run() is fully mocked below,
        # so engine.run(...) is called but never awaited. An AsyncMock here
        # would construct a real coroutine object at call time (before it
        # ever reaches the mocked asyncio.run) that then never gets awaited —
        # a genuine "coroutine was never awaited" leak, just one that only
        # surfaces later, misattributed to whatever test the GC happens to
        # run during.
        mock_engine_cls.return_value.run = MagicMock()

        # Should not raise/propagate.
        main()


def test_ytdlp_task_completion_does_not_clear_album_flag_while_siblings_staged(temp_db, tmp_path):
    """Regression test for the bug where re-running `--staged` kept
    re-downloading already-finished files: staging a whole album sets
    is_staged on both the album row and every asset in it. Finishing one
    asset must clear ONLY that asset's flag — the album's own is_staged
    badge must stay 1 as long as any sibling asset is still staged, and
    get_staged_assets() must never resurface the finished asset.
    """
    engine = DownloadEngine(temp_db)
    reg = temp_db.register_album_from_json({
        "selected_album": {"title": "Two File Album", "album_index_number": 1},
        "files_found": [
            {"href": "https://link.com/f/a", "title": "a.mp4"},
            {"href": "https://link.com/f/b", "title": "b.mp4"},
        ],
    })
    album_id = reg[0]
    assets = temp_db.get_album_assets(album_id)
    asset_a_id, asset_b_id = assets[0]["id"], assets[1]["id"]

    # Mirrors inspector.py's toggle_staging(target="album"): both the album
    # row and every asset in it get is_staged=1.
    with temp_db.connection() as conn:
        conn.execute("UPDATE albums SET is_staged = 1 WHERE id = ?", (album_id,))
        conn.execute("UPDATE assets SET is_staged = 1 WHERE album_id = ?", (album_id,))

    temp_db.get_valid_url = MagicMock(return_value="https://cdn/a")
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    mock_proc = MagicMock()
    mock_proc.stdout = []
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0

    # Finish only asset A.
    asset_a = {"title": "a.mp4", "db_asset_id": asset_a_id, "signed_cdn_url": "https://cdn/a"}
    with patch("subprocess.Popen", return_value=mock_proc):
        success = engine.execute_ytdlp_task(
            index=1, total_files=2, asset_data=asset_a, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )
    assert success is True

    staged_ids = {r["id"] for r in temp_db.get_staged_assets()}
    assert asset_a_id not in staged_ids, "finished asset resurfaced in the staged queue"
    assert asset_b_id in staged_ids, "unfinished sibling asset dropped out of the staged queue"

    album = temp_db.get_album(album_id)
    assert album["is_staged"] == 1, "album badge cleared while a sibling asset is still staged"

    # Now finish asset B too.
    asset_b = {"title": "b.mp4", "db_asset_id": asset_b_id, "signed_cdn_url": "https://cdn/b"}
    with patch("subprocess.Popen", return_value=mock_proc):
        engine.execute_ytdlp_task(
            index=2, total_files=2, asset_data=asset_b, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    assert temp_db.get_staged_assets() == []
    album = temp_db.get_album(album_id)
    assert album["is_staged"] == 0, "album badge should clear once every asset is done"


# ============================================================
# execute_ytdlp_task — failures must be visible in the console, not just
# silently recorded in the DB
# ============================================================


def test_ytdlp_task_no_url_failure_prints_to_console(temp_db, tmp_path):
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "missing.mp4", "db_asset_id": 1}
    temp_db.get_valid_url = MagicMock(return_value=None)
    temp_db.update_download_status = MagicMock()

    engine.execute_ytdlp_task(
        index=1, total_files=1, asset_data=asset, slot_id=0,
        task_id=task_id, progress=mock_progress, output_root=tmp_path,
    )

    printed = [str(c) for c in mock_progress.console.print.call_args_list]
    assert any("Failed" in p and "missing.mp4" in p for p in printed)


def test_ytdlp_task_nonzero_exit_prints_to_console(temp_db, tmp_path):
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "x.mp4", "db_asset_id": 3, "signed_cdn_url": "https://cdn/x"}
    temp_db.get_valid_url = MagicMock(return_value="https://cdn/x")
    temp_db.update_download_status = MagicMock()

    mock_proc = MagicMock()
    mock_proc.stdout = []
    mock_proc.wait.return_value = None
    mock_proc.returncode = 1

    with patch("subprocess.Popen", return_value=mock_proc):
        engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    printed = [str(c) for c in mock_progress.console.print.call_args_list]
    assert any("Failed" in p and "exit code 1" in p for p in printed)


def test_ytdlp_task_popen_exception_prints_to_console(temp_db, tmp_path):
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "x.mp4", "db_asset_id": 4, "signed_cdn_url": "https://cdn/x"}
    temp_db.get_valid_url = MagicMock(return_value="https://cdn/x")
    temp_db.update_download_status = MagicMock()

    with patch("subprocess.Popen", side_effect=OSError("yt-dlp binary missing")):
        engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    printed = [str(c) for c in mock_progress.console.print.call_args_list]
    assert any("Failed" in p and "yt-dlp binary missing" in p for p in printed)


def test_ytdlp_task_popen_exception_during_shutdown_does_not_print_failure(temp_db, tmp_path):
    """A subprocess error caused by shutdown-triggered cleanup isn't a real
    failure — it shouldn't alarm the user with a spurious [Failed] line.
    """
    engine = DownloadEngine(temp_db)
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)
    asset = {"title": "x.mp4", "db_asset_id": 6, "signed_cdn_url": "https://cdn/x"}
    temp_db.get_valid_url = MagicMock(return_value="https://cdn/x")
    temp_db.update_download_status = MagicMock()
    engine.shutdown_event.set()

    with patch("subprocess.Popen", side_effect=OSError("boom")):
        engine.execute_ytdlp_task(
            index=1, total_files=1, asset_data=asset, slot_id=0,
            task_id=task_id, progress=mock_progress, output_root=tmp_path,
        )

    printed = [str(c) for c in mock_progress.console.print.call_args_list]
    assert not any("Failed" in p for p in printed)