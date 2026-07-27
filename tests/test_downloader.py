from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bunkr_api.media.downloader import DownloadEngine


def test_ytdlp_task_success(temp_db, tmp_path):
    """Test successful execution of a yt-dlp task."""
    engine = DownloadEngine(temp_db)

    # Mock progress components
    mock_progress = MagicMock()
    task_id = mock_progress.add_task("test", total=None)

    asset_data = {
        "title": "sample_video.mp4",
        "db_asset_id": 1,
        "signed_cdn_url": "https://cdn.example.com/file.mp4",
        "album_id": "101",
        "album_title": "My Album",
    }

    # Mock DB URL return
    temp_db.get_valid_url = MagicMock(return_value="https://cdn.example.com/file.mp4")
    temp_db.update_download_status = MagicMock()

    # Mock subprocess.Popen
    mock_proc = MagicMock()
    mock_proc.stdout = ["PROGRESS 500 1000\n"]
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0

    # Logic in downloader.py uses get_album_folder_name,
    # which produces "#ID_Title" with underscores.
    expected_folder = "#101_My_Album"
    expected_filename = "sample_video.mp4"

    # Construct the path using Path to handle OS-specific separators (\ vs /)
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
    # Compare strings after forcing the expected_path through str()
    # to match what update_download_status receives.
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


def test_engine_shutdown_event(temp_db):
    """Test that execute_ytdlp_task exits early when the shutdown flag is set."""
    engine = DownloadEngine(temp_db)
    engine.shutdown_event.set()

    # Verify execute_ytdlp_task exits early when shutdown flag is set
    mock_progress = MagicMock()
    asset = {"title": "test.mp4", "db_asset_id": 1, "signed_cdn_url": "http://example.com"}

    temp_db.get_valid_url = MagicMock(return_value="http://example.com")

    success = engine.execute_ytdlp_task(
        1, 1, asset, 0, 1, mock_progress, output_root=MagicMock()
    )

    assert success is False


@pytest.mark.asyncio
async def test_resolve_tokens_graceful_failure(temp_db):
    """Test that resolve_tokens_async doesn't raise when token minting fails."""
    engine = DownloadEngine(temp_db)

    assets = [{"db_asset_id": 1, "true_file_id": "abc", "signed_cdn_url": None}]

    with patch(
        "bunkr_api.media.downloader.mint_single_url_async",
        side_effect=Exception("API Down"),
    ):
        # Should not raise exception
        await engine.resolve_tokens_async(assets)