"""Tests for the `bunkr-api <command>` interface: parser shape, dispatch,
legacy-invocation translation, and the exit-code contract.

The per-command *behaviour* (scraping, downloading, ...) is covered in the
module-specific test files; here we only check that the CLI routes to it.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bunkr_api import cli
from bunkr_api.cli import COMMANDS, _translate_legacy, build_parser, main


@pytest.fixture(autouse=True)
def mock_db():
    with patch("bunkr_api.cli.db") as mock:
        yield mock


@pytest.fixture
def parse():
    return build_parser().parse_args


# ============================================================
# Parser shape
# ============================================================


def test_no_arguments_selects_the_dashboard(parse):
    assert parse([]).func is cli.cmd_dashboard


def test_every_documented_command_exists_and_has_a_handler(parse):
    for name in COMMANDS:
        sample = {"open": ["1"], "import": ["f.json"]}.get(name, [])
        assert callable(parse([name, *sample]).func)


def test_help_lists_every_command_and_mentions_the_dashboard(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["-h"])
    out = capsys.readouterr().out
    assert exc.value.code == 0
    for name in COMMANDS:
        assert name in out
    assert "dashboard" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert "bunkr-api" in capsys.readouterr().out


def test_unknown_command_is_an_error_not_a_search(capsys):
    # Subcommands are explicit: a typo must not silently become a search.
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["scrpae", "x"])
    assert exc.value.code == 2


# ---- scrape ----------------------------------------------------------------


def test_scrape_parses_search_and_options(parse):
    a = parse(["scrape", "some name", "-m", "strict", "-p", "40", "-s", "latest", "--save-json"])
    assert (a.search, a.mode, a.per, a.sort, a.save_json) == ("some name", "strict", 40, "latest", True)
    assert a.top is None


def test_scrape_top_with_category(parse):
    assert parse(["scrape", "--top", "albums"]).top == "albums"


def test_scrape_top_without_category_means_ask(parse):
    assert parse(["scrape", "--top"]).top is True


def test_scrape_rejects_unknown_top_category():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["scrape", "--top", "nonsense"])


def test_scrape_search_and_top_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["scrape", "query", "--top", "albums"])


def test_scrape_output_is_a_path(parse):
    assert parse(["scrape", "-o", "out/json"]).output == Path("out/json")


# ---- download / stream -----------------------------------------------------


def test_download_parses_options(parse):
    a = parse(["download", "--db-id", "5", "-w", "3", "-n", "1-4", "-o", "out"])
    assert (a.db_id, a.workers, a.number, a.output) == (5, 3, "1-4", "out")


@pytest.mark.parametrize(
    "flags",
    [["--staged", "--db-id", "5"], ["--triage", "--staged"], ["-i", "f.json", "--db-id", "1"]],
)
def test_download_sources_are_mutually_exclusive(flags):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["download", *flags])


def test_stream_parses_options(parse):
    a = parse(["stream", "--db-id", "5", "-n", "2", "--player", "vlc"])
    assert (a.db_id, a.number, a.player) == (5, "2", "vlc")


def test_stream_rejects_unknown_player():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["stream", "--player", "winamp"])


def test_stream_sources_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["stream", "--staged", "--db-id", "2"])


# ---- open / import / mint / inspect ---------------------------------------


def test_open_requires_a_numeric_id():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["open"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["open", "abc"])


def test_open_parses_id(parse):
    assert parse(["open", "17"]).album_id == 17


def test_import_requires_a_file():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["import"])


def test_mint_defaults_to_daemon_and_accepts_album_id(parse):
    assert parse(["mint"]).album_id is None
    assert parse(["mint", "-a", "3"]).album_id == 3


def test_inspect_nested_subcommands_do_not_clash_with_top_level(parse):
    a = parse(["inspect", "view", "albums"])
    assert a.command == "inspect"
    assert a.inspect_cmd == "view"
    assert a.target == "albums"

    a = parse(["inspect", "stage", "album", "5", "--off"])
    assert (a.inspect_cmd, a.target, a.selection, a.off) == ("stage", "album", "5", True)

    a = parse(["inspect", "db", "--wipe", "7-9", "-y"])
    assert (a.inspect_cmd, a.wipe, a.yes) == ("db", "7-9", True)


def test_inspect_without_subcommand_parses(parse):
    assert parse(["inspect"]).inspect_cmd is None


# ============================================================
# Dispatch
# ============================================================


def test_main_without_arguments_runs_the_dashboard():
    with patch("bunkr_api.cli.main_loop", new_callable=AsyncMock) as loop:
        assert main([]) == 0
    loop.assert_awaited_once()


def test_dashboard_ctrl_c_exits_cleanly():
    with patch("bunkr_api.cli.main_loop", new_callable=AsyncMock, side_effect=KeyboardInterrupt):
        assert main([]) == 0


def test_main_reads_sys_argv_by_default():
    with (
        patch("sys.argv", ["bunkr-api", "open", "9"]),
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show,
    ):
        assert main() == 0
    show.assert_awaited_once_with(9)


def test_open_dispatches_to_album_view():
    with patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show:
        assert main(["open", "17"]) == 0
    show.assert_awaited_once_with(17)


def test_scrape_search_dispatches_then_opens_the_chosen_album():
    with (
        patch("bunkr_api.cli.run_scrape_interactive", new_callable=AsyncMock, return_value=55) as scrape,
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show,
    ):
        assert main(["scrape", "some search", "-m", "strict"]) == 0
    kwargs = scrape.await_args.kwargs
    assert kwargs["search_seed"] == "some search"
    assert kwargs["mode_seed"] is not None
    show.assert_awaited_once_with(55)


def test_scrape_without_a_selection_does_not_open_anything():
    with (
        patch("bunkr_api.cli.run_scrape_interactive", new_callable=AsyncMock, return_value=None),
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show,
    ):
        assert main(["scrape", "nothing"]) == 0
    show.assert_not_awaited()


def test_scrape_top_with_category_dispatches_to_trending():
    with (
        patch("bunkr_api.cli.run_top_engine_interactive", new_callable=AsyncMock, return_value=66) as top,
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show,
    ):
        assert main(["scrape", "--top", "albums"]) == 0
    assert top.await_args.kwargs["category_seed"] == "albums"
    show.assert_awaited_once_with(66)


def test_scrape_top_without_category_asks_interactively():
    with (
        patch("bunkr_api.cli.run_top_engine_interactive", new_callable=AsyncMock, return_value=None) as top,
        patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock),
    ):
        main(["scrape", "--top"])
    assert top.await_args.kwargs["category_seed"] is None


def test_mint_dispatches_to_daemon_loop():
    with patch("bunkr_api.core.tokens.daemon_loop") as daemon:
        assert main(["mint", "-a", "3"]) == 0
    daemon.assert_called_once_with(album_id=3)


def test_stream_dispatches_to_player_logic():
    with patch("bunkr_api.media.player.run_player_logic", new_callable=AsyncMock) as logic:
        assert main(["stream", "--db-id", "5", "-n", "2", "--player", "vlc"]) == 0
    logic.assert_awaited_once_with(5, "2", "vlc", False)


def test_inspect_dispatches_to_inspector():
    with patch("bunkr_api.inspector.Inspector") as insp:
        assert main(["inspect", "view", "albums"]) == 0
    insp.return_value.display_albums.assert_called_once()


def test_inspect_without_subcommand_shows_the_dashboard():
    with patch("bunkr_api.inspector.Inspector") as insp:
        assert main(["inspect"]) == 0
    insp.return_value.display_dashboard.assert_called_once()


def test_ctrl_c_in_a_command_returns_130():
    with patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock, side_effect=KeyboardInterrupt):
        assert main(["open", "1"]) == 130


# ============================================================
# import
# ============================================================


def test_import_registers_then_opens_the_album(mock_db, tmp_path):
    f = tmp_path / "album.json"
    f.write_text(json.dumps({"selected_album": {}, "files_found": []}))
    mock_db.register_album_from_json.return_value = (42, 1, 0)

    with patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show:
        assert main(["import", str(f)]) == 0
    show.assert_awaited_once_with(42)


def test_import_missing_file_fails_with_a_message(tmp_path, capsys):
    assert main(["import", str(tmp_path / "nope.json")]) == 1
    assert "not found" in capsys.readouterr().out.lower()


def test_import_invalid_json_fails(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json{{{")
    with patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show:
        assert main(["import", str(f)]) == 1
    show.assert_not_awaited()


# ============================================================
# Legacy invocations
# ============================================================


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (["17"], ["open", "17"]),
        (["--db-id", "5"], ["open", "5"]),
        (["-i", "a.json"], ["import", "a.json"]),
        (["album.json"], ["import", "album.json"]),
        (["some name"], ["scrape", "some name"]),
        (["Some Name", "-m", "strict", "-p", "40"], ["scrape", "Some Name", "-m", "strict", "-p", "40"]),
        (["--top", "albums"], ["scrape", "--top", "albums"]),
        (["--save-json", "-o", "x"], ["scrape", "--save-json", "-o", "x"]),
        # a numeric *option value* must never be mistaken for an album id
        (["--per", "40"], ["scrape", "--per", "40"]),
    ],
)
def test_legacy_invocations_translate_to_subcommands(old, new):
    argv, hint = _translate_legacy(old)
    assert argv == new
    assert hint is not None
    assert hint.startswith("bunkr-api ")


@pytest.mark.parametrize(
    "argv",
    [[], ["-h"], ["--help"], ["-v"], ["--version"], *([c] for c in COMMANDS), ["scrape", "17"]],
)
def test_modern_invocations_pass_through_untouched(argv):
    assert _translate_legacy(argv) == (argv, None)


def test_legacy_hint_quotes_arguments_with_spaces():
    _, hint = _translate_legacy(["two words"])
    assert hint == "bunkr-api scrape 'two words'"


def test_legacy_form_runs_and_warns_on_stderr(capsys):
    with patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock) as show:
        assert main(["17"]) == 0
    show.assert_awaited_once_with(17)
    err = capsys.readouterr().err
    assert "deprecated" in err
    assert "bunkr-api open 17" in err


def test_modern_form_prints_no_deprecation_notice(capsys):
    with patch("bunkr_api.cli.show_album_details", new_callable=AsyncMock):
        main(["open", "17"])
    assert capsys.readouterr().err == ""


def test_legacy_form_with_bad_option_value_still_reports_an_error():
    with pytest.raises(SystemExit) as exc:
        main(["query", "--per", "7"])
    assert exc.value.code == 2


# ============================================================
# download / stream: exit codes and the non-interactive guard
# ============================================================


@pytest.fixture
def no_tty():
    with patch("sys.stdin", MagicMock(isatty=lambda: False)):
        yield


@pytest.fixture
def temp_db_factory(temp_db):
    with patch("bunkr_api.core.db.DatabaseManager", return_value=temp_db):
        yield temp_db


def test_download_without_a_source_fails_fast_when_not_a_terminal(no_tty, temp_db_factory):
    with patch("bunkr_api.media.downloader.prompt_for_inputs") as prompt:
        assert main(["download"]) == 2
    prompt.assert_not_called()


def test_download_without_a_source_prompts_on_a_terminal(temp_db_factory):
    with (
        patch("sys.stdin", MagicMock(isatty=lambda: True)),
        patch(
            "bunkr_api.media.downloader.prompt_for_inputs",
            return_value=(None, None, None, 1, True, False),
        ) as prompt,
    ):
        assert main(["download"]) == 0  # nothing staged -> nothing to do
    prompt.assert_called_once()


def test_download_unknown_album_is_an_error(no_tty, temp_db_factory):
    assert main(["download", "--db-id", "999"]) == 1


def test_download_with_nothing_staged_is_not_an_error(no_tty, temp_db_factory):
    with patch("bunkr_api.media.downloader.DownloadEngine") as engine:
        assert main(["download", "--staged"]) == 0
    engine.return_value.run.assert_not_called()


def test_download_missing_input_file_is_an_error(no_tty, temp_db_factory, tmp_path):
    assert main(["download", "-i", str(tmp_path / "missing.json")]) == 1


def test_download_bad_selection_is_a_usage_error(no_tty, temp_db_factory, tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"selected_album": {}, "files_found": [{"title": "x", "signed_cdn_url": "u"}]}))
    with patch("bunkr_api.media.downloader.DownloadEngine"):
        assert main(["download", "-i", str(f), "-n", "99"]) == 2


def test_download_runs_the_engine_with_parsed_options(no_tty, temp_db_factory, tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"selected_album": {"title": "T"}, "files_found": [{"title": "x", "signed_cdn_url": "u"}]}))
    with patch("bunkr_api.media.downloader.DownloadEngine") as engine:
        engine.return_value.run = AsyncMock()
        assert main(["download", "-i", str(f), "-w", "4", "-o", str(tmp_path / "out")]) == 0
    call = engine.return_value.run.await_args
    assert len(call.args[0]) == 1
    assert call.kwargs["workers"] == 4
    assert call.kwargs["output_dir"] == tmp_path / "out"


def test_stream_without_a_source_fails_fast_when_not_a_terminal(no_tty):
    with patch("bunkr_api.media.player.run_player_logic", new_callable=AsyncMock) as logic:
        assert main(["stream"]) == 2
    logic.assert_not_awaited()


def test_inspect_view_album_without_id_is_a_usage_error(capsys):
    with patch("bunkr_api.inspector.Inspector"):
        assert main(["inspect", "view", "album"]) == 2
    assert "--id required" in capsys.readouterr().out


def test_inspect_db_without_an_action_is_a_usage_error():
    with patch("bunkr_api.inspector.Inspector"):
        assert main(["inspect", "db"]) == 2


# ============================================================
# Legacy console scripts (bunkr-scrape, bunkr-download, ...)
# ============================================================


@pytest.mark.parametrize(
    ("module", "command"),
    [
        ("bunkr_api.core.scraper", "scrape"),
        ("bunkr_api.media.downloader", "download"),
        ("bunkr_api.media.player", "stream"),
        ("bunkr_api.core.tokens", "mint"),
        ("bunkr_api.inspector", "inspect"),
    ],
)
def test_legacy_binaries_forward_to_the_matching_subcommand(module, command):
    import importlib

    mod = importlib.import_module(module)
    with (
        patch("sys.argv", ["old-binary", "--flag", "value"]),
        patch("bunkr_api.cli.main", return_value=0) as cli_main,
    ):
        assert mod.main() == 0
    cli_main.assert_called_once_with([command, "--flag", "value"])
