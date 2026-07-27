import pytest

def test_album_registration(temp_db):
    sample_data = {
        "selected_album": {"title": "Test Album", "album_index_number": 5},
        "files_found": [
            {
                "href": "https://link.com/f/1", 
                "title": "file1.mp4", 
                "size": 100, 
                "true_file_id": 123  
            }
        ]
    }
    album_id = temp_db.register_album_from_json(sample_data)
    assert album_id == 1
    
    assets = temp_db.get_album_assets(album_id)
    assert len(assets) == 1
    assert assets[0]["true_file_id"] == 123 

def test_get_failed_and_staged_assets(temp_db):
    # Register an album and update status
    sample_data = {
        "selected_album": {"title": "Test Album", "album_index_number": 1},
        "files_found": [{"true_file_id": 100, "name": "f1.mp4", "size": 50}]
    }
    album_id = temp_db.register_album_from_json(sample_data)
    assets = temp_db.get_album_assets(album_id)
    asset_id = assets[0]["id"]
    
    temp_db.update_download_status(asset_id, "FAILED", error="404 Not Found")
    
    with temp_db.connection() as conn:
        failed = conn.execute("SELECT * FROM assets WHERE download_status = 'FAILED'").fetchall()
        assert len(failed) == 1
        assert failed[0]["id"] == asset_id