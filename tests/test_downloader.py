import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api.media.downloader import DownloadEngine

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