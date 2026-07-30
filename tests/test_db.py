import time
from unittest.mock import patch

# ============================================================
# NOTE ON extract_expiry_from_url
# ============================================================
# db.py calls the REAL extract_expiry_from_url() throughout (no mocking) —
# it reads the `ex` query param off signed_cdn_url and safely returns None
# for missing/malformed/absent URLs (confirmed against utils/formatting.py).
#
# NOTE ON register_album_from_json's RETURN SHAPE
# Returns (album_id, new_file_count, updated_file_count) — a 3-tuple, not
# a bare int. Every call site below unpacks all three.
#
# NOTE ON ALBUM IDENTITY
# Identity is now keyed on the album's stable URL slug (album_slug), not
# title+index — this is the actual fix for the "re-scraping an album wipes
# its files" bug we chased earlier (album #17: a real scrape with a real
# file count somehow landed with 0 assets in the DB). Title+index shifts
# between scrapes due to pagination/sort/new uploads; the slug doesn't.
# Legacy JSON imports without a slug still fall back to title+index keying
# as a best-effort compatibility path.
# ============================================================


def _album_payload(title, slug=None, index=1, files=None, aggregate_size="0 MB"):
    payload = {
        "selected_album": {
            "title": title,
            "album_index_number": index,
            "aggregate_size": aggregate_size,
        },
        "files_found": files or [],
    }
    if slug is not None:
        payload["selected_album"]["album_slug"] = slug
    return payload


# ============================================================
# ALBUM / ASSET REGISTRATION
# ============================================================

def test_register_album_creates_album_and_assets(temp_db):
    data = _album_payload(
        "Test Album", slug="test-album-abc",
        files=[{"href": "https://link.com/f/1", "title": "file1.mp4", "size": 100, "true_file_id": 123}],
    )

    album_id, new_count, updated_count = temp_db.register_album_from_json(data)

    assert album_id == 1
    assert new_count == 1
    assert updated_count == 0

    assets = temp_db.get_album_assets(album_id)
    assert len(assets) == 1
    assert assets[0]["true_file_id"] == 123
    assert assets[0]["title"] == "file1.mp4"


def test_register_album_true_file_id_falls_back_to_slug_id(temp_db):
    data = _album_payload(
        "Slug Fallback Album", slug="slug-fallback",
        files=[{"href": "https://link.com/f/2", "title": "f.mp4", "slug_id": 777}],
    )

    album_id, _, _ = temp_db.register_album_from_json(data)

    assets = temp_db.get_album_assets(album_id)
    assert assets[0]["true_file_id"] == 777


def test_register_album_same_slug_reuses_id_even_when_title_and_index_shift(temp_db):
    """This is the actual regression test for the bug we chased earlier:
    the same album resurfacing at a different search-result position (and
    even a slightly different title) on a later scrape must NOT register
    as a new, duplicate album with its own empty asset list.
    """
    first = _album_payload(
        "Cool Album", slug="cool-album-xyz", index=5,
        files=[{"href": "https://link.com/f/a", "title": "a.mp4"}],
        aggregate_size="1 GB",
    )
    second = _album_payload(
        "Cool Album (Updated Title)", slug="cool-album-xyz", index=12,
        files=[
            {"href": "https://link.com/f/a", "title": "a.mp4"},
            {"href": "https://link.com/f/b", "title": "b.mp4"},
        ],
        aggregate_size="2 GB",
    )

    first_id, first_new, first_updated = temp_db.register_album_from_json(first)
    second_id, second_new, second_updated = temp_db.register_album_from_json(second)

    assert first_id == second_id  # same slug -> same album row, not a duplicate
    assert first_new == 1
    assert first_updated == 0
    assert second_new == 1        # file "b" is genuinely new
    assert second_updated == 1    # file "a" already existed -> updated, not duplicated

    all_albums = temp_db.get_all_albums()
    assert len(all_albums) == 1
    assert all_albums[0]["aggregate_size"] == "2 GB"
    assert all_albums[0]["file_count"] == 2

    assets = temp_db.get_album_assets(first_id)
    assert len(assets) == 2  # not 3 — file "a" was updated in place, not duplicated


def test_register_album_no_slug_falls_back_to_legacy_title_index_identity(temp_db):
    """Legacy JSON imports that predate album_slug fall back to the old
    title+index keying — best effort only, since there's nothing more
    stable to go on for those.
    """
    data = {
        "selected_album": {"title": "Legacy Album", "album_index_number": 1},
        "files_found": [{"href": "https://link.com/f/legacy", "title": "l.mp4"}],
    }

    first_id, _, _ = temp_db.register_album_from_json(data)
    second_id, _, _ = temp_db.register_album_from_json(data)  # re-import same legacy payload

    assert first_id == second_id
    assert len(temp_db.get_all_albums()) == 1


def test_register_album_asset_reassigned_when_source_url_moves_to_new_album(temp_db):
    """The asset upsert's ON CONFLICT now also updates album_id — so if a
    source_url shows up again under a DIFFERENT album's registration, the
    asset is reassigned to the new album rather than silently staying
    attached to its original one.
    """
    album_a = _album_payload(
        "Album A", slug="album-a",
        files=[{"href": "https://link.com/f/shared", "title": "shared.mp4"}],
    )
    album_b = _album_payload(
        "Album B", slug="album-b", index=2,
        files=[{"href": "https://link.com/f/shared", "title": "shared.mp4"}],
    )

    album_a_id, _, _ = temp_db.register_album_from_json(album_a)
    album_b_id, _, _ = temp_db.register_album_from_json(album_b)

    assert temp_db.get_album_assets(album_a_id) == []
    reassigned = temp_db.get_album_assets(album_b_id)
    assert len(reassigned) == 1
    assert reassigned[0]["source_url"] == "https://link.com/f/shared"


def test_register_album_new_and_updated_counts_on_partial_overlap(temp_db):
    first = _album_payload(
        "Overlap Album", slug="overlap-album",
        files=[
            {"href": "https://link.com/f/x", "title": "x.mp4"},
            {"href": "https://link.com/f/y", "title": "y.mp4"},
        ],
    )
    second = _album_payload(
        "Overlap Album", slug="overlap-album",
        files=[
            {"href": "https://link.com/f/y", "title": "y.mp4"},  # already exists -> updated
            {"href": "https://link.com/f/z", "title": "z.mp4"},  # brand new
        ],
    )

    temp_db.register_album_from_json(first)
    _, new_count, updated_count = temp_db.register_album_from_json(second)

    assert new_count == 1
    assert updated_count == 1


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
    data = _album_payload(
        "Status Album", slug="status-album",
        files=[{"href": "https://link.com/f/1", "title": "f1.mp4", "true_file_id": 100}],
    )
    album_id, _, _ = temp_db.register_album_from_json(data)
    asset_id = temp_db.get_album_assets(album_id)[0]["id"]

    temp_db.update_download_status(asset_id, "FAILED", error="404 Not Found")

    asset = temp_db.get_album_assets(album_id)[0]
    assert asset["download_status"] == "FAILED"
    assert asset["error_message"] == "404 Not Found"


def test_update_download_status_overwrites_previous_error_and_path(temp_db):
    """Documents current behavior: each call unconditionally overwrites
    local_file_path and error_message with whatever (or nothing) is passed,
    so a later status update with no error wipes out a prior failure message.
    """
    data = _album_payload(
        "Overwrite Album", slug="overwrite-album",
        files=[{"href": "https://link.com/f/1", "title": "f1.mp4"}],
    )
    album_id, _, _ = temp_db.register_album_from_json(data)
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

def _register_asset(temp_db, href, expiry_ts, true_file_id=1, album_title="Refresh Album", album_slug="refresh-album"):
    """Helper: registers one asset with a REAL signed_cdn_url.

    If expiry_ts is given, it's embedded as a genuine `?ex=` query param so
    the real extract_expiry_from_url() parses it exactly as it would in
    production. If expiry_ts is None, no `ex` param is included at all.
    """
    signed_cdn_url = f"https://cdn.example.com/{true_file_id}"
    if expiry_ts is not None:
        signed_cdn_url += f"?ex={expiry_ts}"

    data = _album_payload(
        album_title, slug=album_slug,
        files=[{"href": href, "title": "f.mp4", "true_file_id": true_file_id, "signed_cdn_url": signed_cdn_url}],
    )
    album_id, _, _ = temp_db.register_album_from_json(data)
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
        temp_db, "https://link.com/f/a", None, true_file_id=1, album_title="Album A", album_slug="album-a-refresh"
    )
    album_b, asset_b = _register_asset(
        temp_db, "https://link.com/f/b", None, true_file_id=2, album_title="Album B", album_slug="album-b-refresh"
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


# ============================================================
# get_album / delete_album
# ============================================================

def test_get_album_returns_row_when_found(temp_db):
    album_id = _seed_album_helper(temp_db)
    album = temp_db.get_album(album_id)
    assert album is not None
    assert album["id"] == album_id


def test_get_album_returns_none_when_missing(temp_db):
    assert temp_db.get_album(99999) is None


def test_delete_album_removes_album_and_cascades_to_assets(temp_db):
    data = _album_payload(
        "Delete Me", slug="delete-me",
        files=[{"href": "https://link.com/f/1", "title": "f1.mp4"}],
    )
    album_id, _, _ = temp_db.register_album_from_json(data)

    deleted = temp_db.delete_album(album_id)

    assert deleted is True
    assert temp_db.get_album(album_id) is None
    assert temp_db.get_album_assets(album_id) == []


def test_delete_album_returns_false_when_nothing_to_delete(temp_db):
    assert temp_db.delete_album(99999) is False


# ============================================================
# get_staged_assets / get_failed_assets
# ============================================================

def test_get_staged_assets_includes_directly_and_album_staged(temp_db):
    direct_id = _seed_album_helper(temp_db, title="Direct Stage", slug="direct-stage",
                                    files=[{"href": "https://link.com/f/direct", "title": "d.mp4"}])
    album_staged_id = _seed_album_helper(temp_db, title="Album Stage", slug="album-stage",
                                          files=[{"href": "https://link.com/f/album", "title": "a.mp4"}])
    unstaged_id = _seed_album_helper(temp_db, title="Untouched", slug="untouched",  # noqa: F841
                                      files=[{"href": "https://link.com/f/none", "title": "n.mp4"}])

    direct_asset = temp_db.get_album_assets(direct_id)[0]
    with temp_db.connection() as conn:
        conn.execute("UPDATE assets SET is_staged=1 WHERE id=?", (direct_asset["id"],))
        conn.execute("UPDATE albums SET is_staged=1 WHERE id=?", (album_staged_id,))

    staged = temp_db.get_staged_assets()
    titles = {row["title"] for row in staged}

    assert "d.mp4" in titles
    assert "a.mp4" in titles
    assert "n.mp4" not in titles
    # confirms the JOIN actually attaches album_title
    assert all(row["album_title"] for row in staged)


def test_get_failed_assets_only_returns_failed_status(temp_db):
    album_id = _seed_album_helper(temp_db, files=[
        {"href": "https://link.com/f/ok", "title": "ok.mp4"},
        {"href": "https://link.com/f/bad", "title": "bad.mp4"},
    ])
    assets = temp_db.get_album_assets(album_id)
    temp_db.update_download_status(assets[0]["id"], "COMPLETED", "/tmp/ok.mp4")  # noqa: S108
    temp_db.update_download_status(assets[1]["id"], "FAILED", error="404")

    failed = temp_db.get_failed_assets()
    titles = {row["title"] for row in failed}

    assert titles == {"bad.mp4"}


def _seed_album_helper(db, title="Helper Album", slug="helper-album", files=None):
    data = _album_payload(title, slug=slug, files=files or [])
    album_id, _, _ = db.register_album_from_json(data)
    return album_id