from unittest.mock import MagicMock, patch

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
        1, "COMPLETED", str(tmp_path / "101_My Album" / "sample_video.mp4")
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