import time
from unittest.mock import MagicMock, patch

import pytest

from bunkr_api.inspector import Inspector, main

# ============================================================
# NOTE ON THE `insp` FIXTURE
# ============================================================
# Inspector.__init__ constructs its own real DatabaseManager() at the
# DEFAULT db path — same class of import/construction-time side effect
# we've seen elsewhere. Since this file is heavily SQL-dependent (COUNT,
# SUM, GROUP BY, ALTER TABLE, DROP TABLE...), mocking every query would
# mostly just test the mocks. Instead we bypass __init__ entirely via
# Inspector.__new__() and inject the existing `temp_db` fixture (a real
# DatabaseManager backed by a tmp_path sqlite file) as `.db` — giving
# genuine SQL semantics without touching the real default database.
# ============================================================


@pytest.fixture
def insp(temp_db):
    inspector = Inspector.__new__(Inspector)
    inspector.db = temp_db
    return inspector


def _seed_album(db, title="Test Album", slug="test-album", files=None, aggregate_size="10 MB"):
    data = {
        "selected_album": {
            "title": title, "album_index_number": 1, "album_slug": slug,
            "aggregate_size": aggregate_size,
        },
        "files_found": files or [],
    }
    album_id, _, _ = db.register_album_from_json(data)
    return album_id


# ============================================================
# display_table
# ============================================================

def test_display_table_no_rows(insp, capsys):
    insp.display_table("albums")
    assert "No records found" in capsys.readouterr().out


def test_display_table_shows_rows(insp, capsys):
    _seed_album(insp.db, files=[{"href": "https://x/1", "title": "f1.mp4"}])
    insp.display_table("assets")
    out = capsys.readouterr().out
    assert "row(s):" in out
    assert "f1.mp4" in out


def test_display_table_search_filters_by_title(insp, capsys):
    _seed_album(insp.db, files=[{"href": "https://x/a", "title": "matchme.mp4"}])
    insp.display_table("assets", search="matchme")
    assert "matchme.mp4" in capsys.readouterr().out

    insp.display_table("assets", search="nomatch")
    assert "No records found" in capsys.readouterr().out


def test_display_table_respects_limit(insp, capsys):
    files = [{"href": f"https://x/{i}", "title": f"f{i}.mp4"} for i in range(5)]
    _seed_album(insp.db, files=files)
    insp.display_table("assets", limit=2)
    assert "2 row(s):" in capsys.readouterr().out


def test_display_table_show_all_ignores_limit(insp, capsys):
    files = [{"href": f"https://x/{i}", "title": f"f{i}.mp4"} for i in range(5)]
    _seed_album(insp.db, files=files)
    insp.display_table("assets", limit=2, show_all=True)
    assert "5 row(s):" in capsys.readouterr().out


def test_display_table_invalid_table_name_shows_query_error(insp, capsys):
    insp.display_table("not_a_real_table")
    assert "Query error" in capsys.readouterr().out


# ============================================================
# display_dashboard
# ============================================================

def test_display_dashboard_shows_table_names_and_metrics(insp, capsys):
    _seed_album(insp.db, files=[{"href": "https://x/1", "title": "f1.mp4"}])
    insp.display_dashboard()
    out = capsys.readouterr().out
    assert "albums" in out
    assert "assets" in out
    assert "system_config" in out
    assert "Done:" in out and "Fail:" in out and "Staged:" in out
    assert "max_workers" in out  # seeded config key


# ============================================================
# display_albums
# ============================================================

def test_display_albums_no_albums(insp, capsys):
    insp.display_albums()
    assert "No albums cataloged" in capsys.readouterr().out


def test_display_albums_shows_completion_and_failures(insp, capsys):
    album_id = _seed_album(insp.db, files=[
        {"href": "https://x/1", "title": "f1.mp4"},
        {"href": "https://x/2", "title": "f2.mp4"},
    ])
    assets = insp.db.get_album_assets(album_id)
    insp.db.update_download_status(assets[0]["id"], "COMPLETED", "/tmp/f1.mp4")  # noqa: S108
    insp.db.update_download_status(assets[1]["id"], "FAILED", error="oops")

    insp.display_albums()
    out = capsys.readouterr().out
    assert "50%" in out
    assert "!1" in out  # 1 failure flagged


# ============================================================
# display_album_detail
# ============================================================

def test_display_album_detail_not_found(insp, capsys):
    insp.display_album_detail(999)
    assert "not found" in capsys.readouterr().out


def test_display_album_detail_shows_album_and_assets(insp, capsys):
    album_id = _seed_album(insp.db, title="Detail Album", files=[{"href": "https://x/1", "title": "detail.mp4"}])
    insp.display_album_detail(album_id)
    out = capsys.readouterr().out
    assert f"Album Detail: #{album_id}" in out
    assert "detail.mp4" in out


# ============================================================
# display_expiring
# ============================================================

def test_display_expiring_none_needs_refresh(insp, capsys):
    insp.display_expiring()
    assert "All tokens are fresh" in capsys.readouterr().out


def test_display_expiring_shows_expired_and_no_token(insp, capsys):
    now = int(time.time())
    _seed_album(insp.db, files=[
        {"href": "https://x/1", "title": "no_token.mp4"},  # no signed_cdn_url -> no expiry
        {"href": "https://x/2", "title": "expired.mp4", "signed_cdn_url": f"https://cdn/2?ex={now - 100}"},
    ])
    insp.display_expiring()
    out = capsys.readouterr().out
    assert "No Token" in out
    assert "Expired" in out


# ============================================================
# display_staged
# ============================================================

def test_display_staged_nothing_staged(insp, capsys):
    insp.display_staged()
    out = capsys.readouterr().out
    assert "No albums flagged staged" in out
    assert "No staged assets" in out


def test_display_staged_shows_staged_albums_and_assets(insp, capsys):
    album_id = _seed_album(insp.db, title="Staged Album", files=[{"href": "https://x/1", "title": "s1.mp4"}])
    with insp.db.connection() as conn:
        conn.execute("UPDATE albums SET is_staged=1 WHERE id=?", (album_id,))
        conn.execute("UPDATE assets SET is_staged=1 WHERE album_id=?", (album_id,))

    insp.display_staged()
    out = capsys.readouterr().out
    assert "Staged Album" in out
    assert "Totals:" in out


# ============================================================
# add_column / drop_column
# ============================================================

def test_add_column_invalid_spec(insp, capsys):
    insp.add_column("not_a_valid_spec")
    assert "Expected table:column:type" in capsys.readouterr().out


def test_add_column_success(insp, capsys):
    insp.add_column("assets:custom_field:TEXT")
    assert "Added" in capsys.readouterr().out
    with insp.db.connection() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets);").fetchall()]
    assert "custom_field" in cols


def test_add_column_already_exists_is_skipped_not_crashed(insp, capsys):
    insp.add_column("assets:dup_field:TEXT")
    capsys.readouterr()
    insp.add_column("assets:dup_field:TEXT")  # second time -> already exists
    assert "Skipped" in capsys.readouterr().out


def test_drop_column_invalid_spec(insp, capsys):
    insp.drop_column("not_valid")
    assert "Expected table:column" in capsys.readouterr().out


def test_drop_column_requires_confirmation_when_not_forced(insp):
    with patch("bunkr_api.inspector.Confirm.ask", return_value=False):
        insp.drop_column("assets:track_number", force=False)
    with insp.db.connection() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets);").fetchall()]
    assert "track_number" in cols  # not dropped


def test_drop_column_force_skips_confirmation(insp, capsys):
    insp.drop_column("assets:track_number", force=True)
    assert "Dropped" in capsys.readouterr().out
    with insp.db.connection() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets);").fetchall()]
    assert "track_number" not in cols


def test_drop_column_nonexistent_column_shows_failed(insp, capsys):
    insp.drop_column("assets:does_not_exist", force=True)
    assert "Failed" in capsys.readouterr().out


# ============================================================
# _resolve_bound
# ============================================================

def test_resolve_bound_empty_table_returns_fallback(insp):
    with insp.db.connection() as conn:
        bound = insp._resolve_bound(conn, "albums")
    assert bound == 1000


def test_resolve_bound_returns_max_id(insp):
    _seed_album(insp.db)
    with insp.db.connection() as conn:
        bound = insp._resolve_bound(conn, "albums")
    assert bound == 1


# ============================================================
# toggle_staging
# ============================================================

def test_toggle_staging_stage_album_cascades_to_assets(insp, capsys):
    album_id = _seed_album(insp.db, files=[{"href": "https://x/1", "title": "f1.mp4"}])
    insp.toggle_staging("album", str(album_id), state=1)
    assert "staged 1 album(s)" in capsys.readouterr().out

    with insp.db.connection() as conn:
        album = conn.execute("SELECT is_staged FROM albums WHERE id=?", (album_id,)).fetchone()
        asset = conn.execute("SELECT is_staged FROM assets WHERE album_id=?", (album_id,)).fetchone()
    assert album["is_staged"] == 1
    assert asset["is_staged"] == 1


def test_toggle_staging_unstage_album(insp):
    album_id = _seed_album(insp.db, files=[{"href": "https://x/1", "title": "f1.mp4"}])
    insp.toggle_staging("album", str(album_id), state=1)
    insp.toggle_staging("album", str(album_id), state=0)
    with insp.db.connection() as conn:
        album = conn.execute("SELECT is_staged FROM albums WHERE id=?", (album_id,)).fetchone()
    assert album["is_staged"] == 0


def test_toggle_staging_assets_by_selection(insp):
    album_id = _seed_album(insp.db, files=[
        {"href": "https://x/1", "title": "f1.mp4"},
        {"href": "https://x/2", "title": "f2.mp4"},
    ])
    assets = insp.db.get_album_assets(album_id)
    asset_ids = [a["id"] for a in assets]

    insp.toggle_staging("asset", f"{asset_ids[0]}", state=1)

    with insp.db.connection() as conn:
        rows = conn.execute("SELECT id, is_staged FROM assets WHERE album_id=?", (album_id,)).fetchall()
    staged = {r["id"]: r["is_staged"] for r in rows}
    assert staged[asset_ids[0]] == 1
    assert staged[asset_ids[1]] == 0


def test_toggle_staging_invalid_selection_shows_error_not_crash(insp, capsys):
    insp.toggle_staging("asset", "not_a_valid_selection", state=1)
    assert "Error:" in capsys.readouterr().out


# ============================================================
# wipe
# ============================================================

def test_wipe_all_requires_confirmation(insp):
    _seed_album(insp.db)
    with patch("bunkr_api.inspector.Confirm.ask", return_value=False):
        insp.wipe(album_ids=None, force=False)
    assert len(insp.db.get_all_albums()) == 1  # nothing wiped


def test_wipe_all_forced_deletes_everything_but_keeps_config(insp, capsys):
    _seed_album(insp.db)
    insp.wipe(album_ids=None, force=True)
    assert "wiped" in capsys.readouterr().out.lower()
    assert insp.db.get_all_albums() == []
    assert insp.db.get_config_val("max_workers", "?") == "4"  # config preserved


def test_wipe_specific_ids_deletes_only_those(insp):
    a = _seed_album(insp.db, title="Keep Me", slug="keep-me")
    b = _seed_album(insp.db, title="Delete Me", slug="delete-me")

    insp.wipe(album_ids=str(b), force=True)

    remaining_ids = {row["id"] for row in insp.db.get_all_albums()}
    assert a in remaining_ids
    assert b not in remaining_ids


def test_wipe_specific_ids_none_match_shows_message(insp, capsys):
    """Uses a genuinely in-range id that just doesn't correspond to any
    real album row (a gap left by AUTOINCREMENT after a delete), so this
    exercises the intended "none of the requested ids exist" branch —
    not parse_selection's own out-of-range ValueError, which is a
    different (already-documented) code path.
    """
    _seed_album(insp.db, title="A", slug="a")
    gap_id = _seed_album(insp.db, title="B", slug="b")
    with insp.db.connection() as conn:
        conn.execute("DELETE FROM albums WHERE id=?", (gap_id,))
    _seed_album(insp.db, title="C", slug="c")  # pushes MAX(id) past the gap

    insp.wipe(album_ids=str(gap_id), force=True)

    assert "None of the requested album id(s) exist" in capsys.readouterr().out


def test_wipe_invalid_selection_string_raises_valueerror_uncaught(insp):
    """Documents a real inconsistency: toggle_staging catches ValueError
    from a bad parse_selection() input and reports it cleanly, but wipe()'s
    specific-ids branch only catches sqlite3.Error — an invalid selection
    string here propagates out of wipe() uncaught instead of being reported
    as a clean error message like everywhere else in this file.
    """
    _seed_album(insp.db)
    with pytest.raises(ValueError):
        insp.wipe(album_ids="totally_not_valid", force=True)


# ============================================================
# nuke
# ============================================================

def test_nuke_requires_exact_confirmation_text(insp):
    _seed_album(insp.db)
    with patch("bunkr_api.inspector.Prompt.ask", return_value="not nuke"):
        insp.nuke()
    with insp.db.connection() as conn:
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        }
    assert "albums" in tables


def test_nuke_confirmed_drops_all_tables(insp, capsys):
    _seed_album(insp.db)
    with patch("bunkr_api.inspector.Prompt.ask", return_value="nuke"):
        insp.nuke()
    assert "nuked" in capsys.readouterr().out.lower()

    with insp.db.connection() as conn:
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        }
    assert tables == set()


# ============================================================
# recount_file_counts
# ============================================================

def test_recount_file_counts_repairs_drifted_columns(insp, capsys):
    album_id = _seed_album(insp.db, files=[{"href": "https://x/1", "title": "f1.mp4", "size": 1024}])
    with insp.db.connection() as conn:
        conn.execute("UPDATE albums SET file_count=0, aggregate_size='0 MB' WHERE id=?", (album_id,))

    insp.recount_file_counts()

    with insp.db.connection() as conn:
        album = conn.execute(
            "SELECT file_count, aggregate_size FROM albums WHERE id=?", (album_id,)
        ).fetchone()
    assert album["file_count"] == 1
    assert album["aggregate_size"] != "0 MB"
    assert "Recounted" in capsys.readouterr().out


def test_recount_file_counts_handles_sql_error_gracefully(insp, capsys):
    with insp.db.connection() as conn:
        conn.execute("DROP TABLE assets")  # break the query on purpose

    insp.recount_file_counts()  # should not raise

    assert "Recount failed" in capsys.readouterr().out


# ============================================================
# main() — CLI routing
#
# Inspector() is constructed for real inside main(), so the class itself
# is patched to avoid touching the real default database while testing
# argparse routing in isolation.
# ============================================================

def test_main_view_dashboard_default():
    with patch("sys.argv", ["bunkr-inspect", "view"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_dashboard.assert_called_once()


def test_main_view_albums():
    with patch("sys.argv", ["bunkr-inspect", "view", "albums"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_albums.assert_called_once()


def test_main_view_expiring():
    with patch("sys.argv", ["bunkr-inspect", "view", "expiring"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_expiring.assert_called_once()


def test_main_view_staged():
    with patch("sys.argv", ["bunkr-inspect", "view", "staged"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_staged.assert_called_once()


def test_main_view_album_requires_id(capsys):
    with patch("sys.argv", ["bunkr-inspect", "view", "album"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_album_detail.assert_not_called()
    assert "--id required" in capsys.readouterr().out


def test_main_view_album_with_id():
    with patch("sys.argv", ["bunkr-inspect", "view", "album", "--id", "5"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_album_detail.assert_called_once_with(5)


def test_main_view_raw_table_name():
    with patch("sys.argv", ["bunkr-inspect", "view", "assets", "-n", "5", "--search", "foo"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_table.assert_called_once_with(
        "assets", limit=5, search="foo", show_all=False
    )


def test_main_stage_album():
    with patch("sys.argv", ["bunkr-inspect", "stage", "album", "5"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.toggle_staging.assert_called_once_with("album", "5", 1)


def test_main_stage_off():
    with patch("sys.argv", ["bunkr-inspect", "stage", "asset", "5", "--off"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.toggle_staging.assert_called_once_with("asset", "5", 0)


def test_main_db_nuke():
    with patch("sys.argv", ["bunkr-inspect", "db", "--nuke"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.nuke.assert_called_once()


def test_main_db_wipe():
    with patch("sys.argv", ["bunkr-inspect", "db", "--wipe", "1,2", "-y"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.wipe.assert_called_once_with("1,2", True)


def test_main_db_recount():
    with patch("sys.argv", ["bunkr-inspect", "db", "--recount"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.recount_file_counts.assert_called_once()


def test_main_db_add_column():
    with patch("sys.argv", ["bunkr-inspect", "db", "--add-column", "assets:x:TEXT"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.add_column.assert_called_once_with("assets:x:TEXT")


def test_main_db_drop_column():
    with patch("sys.argv", ["bunkr-inspect", "db", "--drop-column", "assets:x", "-y"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.drop_column.assert_called_once_with("assets:x", True)


def test_main_db_exec(capsys):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.rowcount = 3
    mock_insp_instance = MagicMock()
    mock_insp_instance.get_conn.return_value = mock_conn

    with patch("sys.argv", ["bunkr-inspect", "db", "--exec", "DELETE FROM assets WHERE 1=0;"]), \
         patch("bunkr_api.inspector.Inspector", return_value=mock_insp_instance):
        main()

    mock_conn.execute.assert_called_once_with("DELETE FROM assets WHERE 1=0;")
    mock_conn.commit.assert_called_once()
    assert "Rows affected: 3" in capsys.readouterr().out


def test_main_no_subcommand_defaults_to_dashboard():
    with patch("sys.argv", ["bunkr-inspect"]), \
         patch("bunkr_api.inspector.Inspector") as mock_cls:
        main()
    mock_cls.return_value.display_dashboard.assert_called_once()