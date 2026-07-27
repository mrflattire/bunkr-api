import pytest

def test_album_registration(temp_db):
    sample_data = {
        "selected_album": {"title": "Test Album", "album_index_number": 5},
        "files_found": [
            {"href": "https://link.com/f/1", "name": "file1.mp4", "size": 100, "id": "123"}
        ]
    }
    album_id = temp_db.register_album_from_json(sample_data)
    assert album_id == 1
    
    assets = temp_db.get_album_assets(album_id)
    assert len(assets) == 1
    assert assets[0]["true_file_id"] == 123