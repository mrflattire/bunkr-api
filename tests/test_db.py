import time
from unittest.mock import patch

# ============================================================
# NOTE ON extract_expiry_from_url
# ============================================================
# db.py calls the REAL extract_expiry_from_url() throughout (no mocking) —
# it reads the `ex` query param off signed_cdn_url and safely returns None
# for missing/malformed/absent URLs (confirmed against utils/formatting.py).
# So tests that don't care about expiry just omit signed_cdn_url entirely,
# and tests that do care build a real URL with `?ex=<timestamp>`.
#
# `mint_now` IS still mocked: it's imported LOCALLY inside get_valid_url()
# (`from .tokens import mint_now`), which re-fetches the name from its
# source module on every call — so it's patched at its source:
# "bunkr_api.core.tokens.mint_now", not "bunkr_api.core.db.mint_now".
# ============================================================


# ============================================================
# ALBUM / ASSET REGISTRATION
# ============================================================

def test_register_album_creates_album_and_assets(temp_db):
    sample_data = {
        "selected_album": {"title": "Test Album", "album_index_number": 5},
        "files_found": [
            {
                "href": "https://link.com/f/1",
                "title": "file1.mp4",
                "size": 100,
                "true_file_id": 123,
            }
        ],
    }

    album_id = temp_db.register_album_from_json(sample_data)

    assert album_id == 1

    assets = temp_db.get_album_assets(album_id)
    assert len(assets) == 1
    assert assets[0]["true_file_id"] == 123
    assert assets[0]["title"] == "file1.mp4"


def test_register_album_true_file_id_falls_back_to_slug_id(temp_db):
    sample_data = {
        "selected_album": {"title": "Slug Fallback Album", "album_index_number": 1},
        "files_found": [
            {"href": "https://link.com/f/2", "title": "f.mp4", "slug_id": 777}
        ],
    }

    album_id = temp_db.register_album_from_json(sample_data)

    assets = temp_db.get_album_assets(album_id)
    assert assets[0]["true_file_id"] == 777


def test_register_album_upsert_updates_existing_album_not_duplicated(temp_db):
    """Registering the same album (same title + index) twice should update
    the existing row via ON CONFLICT(album_id_str), not insert a second one.
    """
    base_album = {"title": "Repeat Album", "album_index_number": 1}

    first_id = temp_db.register_album_from_json({
        "selected_album": {**base_album, "aggregate_size": "10 MB"},
        "files_found": [{"href": "https://link.com/f/a", "title": "a.mp4"}],
    })
    second_id = temp_db.register_album_from_json({
        "selected_album": {**base_album, "aggregate_size": "20 MB"},
        "files_found": [
            {"href": "https://link.com/f/a", "title": "a.mp4"},
            {"href": "https://link.com/f/b", "title": "b.mp4"},
        ],
    })

    assert first_id == second_id

    all_albums = temp_db.get_all_albums()
    assert len(all_albums) == 1
    assert all_albums[0]["aggregate_size"] == "20 MB"
    assert all_albums[0]["file_count"] == 2


def test_register_album_upsert_updates_existing_asset_not_duplicated(temp_db):
    """Re-registering an asset with the same source_url should update it via
    ON CONFLICT(source_url), not create a duplicate row.
    """
    first_pass = {
        "selected_album": {"title": "Asset Upsert Album", "album_index_number": 1},
        "files_found": [
            {"href": "https://link.com/f/x", "title": "x.mp4", "true_file_id": 1, "signed_cdn_url": "https://cdn/old"}
        ],
    }
    second_pass = {
        "selected_album": {"title": "Asset Upsert Album", "album_index_number": 1},
        "files_found": [
            {"href": "https://link.com/f/x", "title": "x.mp4", "true_file_id": 2, "signed_cdn_url": "https://cdn/new"}
        ],
    }

    album_id = temp_db.register_album_from_json(first_pass)
    temp_db.register_album_from_json(second_pass)

    assets = temp_db.get_album_assets(album_id)
    assert len(assets) == 1
    assert assets[0]["true_file_id"] == 2
    assert assets[0]["signed_cdn_url"] == "https://cdn/new"


# ============================================================
# CONFIG
# ============================================================

def test_get_config_val_returns_seeded_defaults(temp_db):
    assert temp_db.get_config_val("max_workers", "999") == "4"
    assert temp_db.get_config_val("token_buffer_seconds", "999") == "600"
    assert temp_db.get_config_val("minter_poll_interval_seconds", "999") == "60"


def test_get_config_val_falls_back_for_unknown_key(temp_db):
    assert temp_db.get_config_val("does_not_exist", "fallback_value") == "fallback_value"


# ============================================================
# DOWNLOAD STATUS
# ============================================================

def test_update_download_status_persists_status_and_error(temp_db):
    sample_data = {
        "selected_album": {"title": "Status Album", "album_index_number": 1},
        "files_found": [{"href": "https://link.com/f/1", "title": "f1.mp4", "true_file_id": 100}],
    }
    album_id = temp_db.register_album_from_json(sample_data)
    asset_id = temp_db.get_album_assets(album_id)[0]["id"]

    temp_db.update_download_status(asset_id, "FAILED", error="404 Not Found")

    asset = temp_db.get_album_assets(album_id)[0]
    assert asset["download_status"] == "FAILED"
    assert asset["error_message"] == "404 Not Found"


def test_update_download_status_overwrites_previous_error_and_path(temp_db):
    """Documents current behavior: each call unconditionally overwrites
    local_file_path and error_message with whatever (or nothing) is passed,
    so a later status update with no error wipes out a prior failure message.
    If this isn't the intended behavior, this test should change alongside
    the fix in db.py.
    """
    sample_data = {
        "selected_album": {"title": "Overwrite Album", "album_index_number": 1},
        "files_found": [{"href": "https://link.com/f/1", "title": "f1.mp4"}],
    }
    album_id = temp_db.register_album_from_json(sample_data)
    asset_id = temp_db.get_album_assets(album_id)[0]["id"]

    temp_db.update_download_status(asset_id, "FAILED", error="timeout")
    temp_db.update_download_status(asset_id, "DOWNLOADING")  # no error passed

    asset = temp_db.get_album_assets(album_id)[0]
    assert asset["download_status"] == "DOWNLOADING"
    assert asset["error_message"] is None  # previous "timeout" got wiped

    temp_db.update_download_status(asset_id, "COMPLETED", local_path="/tmp/out.mp4")  # noqa: S108

    asset = temp_db.get_album_assets(album_id)[0]
    assert asset["local_file_path"] == "/tmp/out.mp4"  # noqa: S108
    assert asset["error_message"] is None


# ============================================================
# TOKEN REFRESH QUERIES
# ============================================================

def _register_asset(temp_db, href, expiry_ts, true_file_id=1, album_title="Refresh Album", album_index=1):
    """Helper: registers one asset with a REAL signed_cdn_url.

    If expiry_ts is given, it's embedded as a genuine `?ex=` query param so
    the real extract_expiry_from_url() parses it exactly as it would in
    production. If expiry_ts is None, no `ex` param is included at all,
    exercising the "no token" path rather than mocking it.
    """
    signed_cdn_url = f"https://cdn.example.com/{true_file_id}"
    if expiry_ts is not None:
        signed_cdn_url += f"?ex={expiry_ts}"

    data = {
        "selected_album": {"title": album_title, "album_index_number": album_index},
        "files_found": [
            {"href": href, "title": "f.mp4", "true_file_id": true_file_id, "signed_cdn_url": signed_cdn_url}
        ],
    }
    album_id = temp_db.register_album_from_json(data)
    asset = next(a for a in temp_db.get_album_assets(album_id) if a["source_url"] == href)
    return album_id, asset["id"]


def test_get_needs_refresh_includes_null_and_expiring_excludes_valid(temp_db):
    now = int(time.time())

    _, null_id = _register_asset(temp_db, "https://link.com/f/null", None, true_file_id=1)
    _, expiring_id = _register_asset(temp_db, "https://link.com/f/soon", now + 100, true_file_id=2)  # within buffer
    _, valid_id = _register_asset(temp_db, "https://link.com/f/valid", now + 3600, true_file_id=3)  # outside buffer

    needing_refresh = {row["id"] for row in temp_db.get_needs_refresh()}

    assert null_id in needing_refresh
    assert expiring_id in needing_refresh
    assert valid_id not in needing_refresh


def test_get_needs_refresh_filters_by_album_id(temp_db):
    album_a, asset_a = _register_asset(
        temp_db, "https://link.com/f/a", None, true_file_id=1, album_title="Album A", album_index=1
    )
    album_b, asset_b = _register_asset(
        temp_db, "https://link.com/f/b", None, true_file_id=2, album_title="Album B", album_index=2
    )

    results = temp_db.get_needs_refresh(album_id=album_a)
    result_ids = {row["id"] for row in results}

    assert asset_a in result_ids
    assert asset_b not in result_ids


# ============================================================
# get_valid_url
# ============================================================

def test_get_valid_url_returns_cached_url_when_not_expiring_soon(temp_db):
    now = int(time.time())
    _, asset_id = _register_asset(temp_db, "https://link.com/f/cached", now + 3600, true_file_id=1)

    with patch("bunkr_api.core.tokens.mint_now") as mock_mint:
        url = temp_db.get_valid_url(asset_id)

    mock_mint.assert_not_called()
    assert url == f"https://cdn.example.com/1?ex={now + 3600}"


def test_get_valid_url_remints_when_expired(temp_db):
    now = int(time.time())
    _, asset_id = _register_asset(temp_db, "https://link.com/f/expired", now - 100, true_file_id=42)

    fresh_url = f"https://cdn.example.com/fresh?ex={now + 3600}"
    with patch("bunkr_api.core.tokens.mint_now", return_value=fresh_url) as mock_mint:
        url = temp_db.get_valid_url(asset_id)

    mock_mint.assert_called_once_with("42")
    assert url == fresh_url

    # confirm the fresh url (and its real parsed expiry) were persisted
    with temp_db.connection() as conn:
        row = conn.execute(
            "SELECT signed_cdn_url, token_expiry_timestamp FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
    assert row["signed_cdn_url"] == fresh_url
    assert row["token_expiry_timestamp"] == now + 3600


def test_get_valid_url_remints_when_missing(temp_db):
    """No signed_cdn_url and no expiry at all should also trigger a remint."""
    _, asset_id = _register_asset(temp_db, "https://link.com/f/missing", None, true_file_id=7)

    with patch("bunkr_api.core.tokens.mint_now", return_value="https://cdn.example.com/brand-new") as mock_mint:
        url = temp_db.get_valid_url(asset_id)

    mock_mint.assert_called_once_with("7")
    assert url == "https://cdn.example.com/brand-new"


def test_get_valid_url_returns_empty_string_for_unknown_asset(temp_db):
    assert temp_db.get_valid_url(99999) == ""