# inspector.py
"""
Quick CLI to poke at media_tracker.db without writing one-off queries.

Usage:
    python inspector.py                     # zero args: dashboard overview (see below)
    python inspector.py --table assets       # just one table
    python inspector.py --table assets -n 20 # show 20 sample rows instead of the default 5
    python inspector.py --table assets --all # dump every row (careful on big tables)
    python inspector.py --sql "SELECT * FROM assets WHERE download_status='FAILED'"

    # Per-album breakdown: files, completion %, failed count, staged count,
    # and total size — one row per album. Complements the global dashboard,
    # which totals across the whole DB rather than showing per-album detail.
    python inspector.py --albums

    # Which assets are stale or about to be, right now — same lookahead
    # window (token_buffer_seconds) get_needs_refresh() uses, so this shows
    # exactly what a mint pass would act on. Useful precisely BECAUSE it
    # doesn't depend on mint.py's daemon actually being run.
    python inspector.py --expiring

    # Quick glance at everything staged right now — albums and assets
    # both, since album-level and asset-level staging don't cascade
    # back up to each other (staging an asset directly doesn't mark
    # its album staged, and vice versa doesn't show here as a diff view).
    python inspector.py --staged

    # Everything about ONE album — its own record + every asset, dumped in
    # full (untruncated URLs, same flat style as --table). --albums summarizes
    # across every album; --table assets --all dumps every album's assets
    # together with no way to isolate one; this fills that gap.
    python inspector.py --db-id 11

    # Dashboard (zero args only — any flag at all skips it in favor of the
    # standard table dump). Shows row counts + pipeline metrics per table.
    # The "Staged" metrics need an is_staged column that doesn't ship with
    # the base schema; run these once before relying on that number:
    #   python inspector.py --add-column assets:is_staged:INTEGER
    #   python inspector.py --add-column albums:is_staged:INTEGER
    # Without it, the dashboard still renders — Staged just shows "n/a"
    # instead of erroring or blanking out the other metrics on that row.
    python inspector.py

    # Toggle staging (requires the is_staged migrations above to have run).
    # SELECTION accepts a single id, comma list, range, or 'all' — same
    # syntax as download.py's picker prompts.
    #
    # --stage-album / --unstage-album cascade: staging an album also stages
    # every asset that belongs to it (both UPDATEs run in one transaction).
    # --stage-assets / --unstage-assets operate on assets directly, with no
    # album scoping — 'all' means every asset in the whole DB, not one album.
    python inspector.py --stage-album 2          # stages album 2 + all its assets
    python inspector.py --unstage-album 2
    python inspector.py --stage-assets 14,15,22
    python inspector.py --stage-assets 1-10
    python inspector.py --unstage-assets all      # clears staging on every asset, globally

    # Write operations (DELETE/UPDATE/VACUUM/etc) — these commit for real,
    # unlike --sql above which is read-only (.fetchall(), never commits).
    python inspector.py --exec "DELETE FROM assets WHERE download_status='FAILED';"
    python inspector.py --exec "UPDATE assets SET token_expiry_timestamp = NULL;"

    # Schema migrations: add a column to an existing table. Replaces the old
    # standalone upgrade_db.py — safe to re-run, skips if column exists.
    python inspector.py --add-column assets:true_file_id:INTEGER

    # Opposite: drop a column. Requires SQLite 3.35+ (bundled with Python
    # 3.9+). Destructive — confirms by default, -y skips the prompt.
    python inspector.py --drop-column assets:true_file_id

    # "Start fresh": clears albums + assets (cascades via FK), keeps
    # system_config (poll intervals, max_workers, etc) intact, then VACUUMs
    # to reclaim disk space. Schema itself is untouched — DatabaseManager's
    # _init_db() would just recreate it anyway (CREATE TABLE IF NOT EXISTS),
    # so there's nothing to "rebuild" here, only data to clear.
    python inspector.py --wipe

    # Scoped version: delete one or more albums + their assets, leave
    # everything else alone. Accepts a single id, comma list, or range —
    # 'all' is intentionally NOT supported here, use --wipe for that.
    # No auto-VACUUM (unlike --wipe/--nuke) since that cost scales with the
    # whole DB, not the rows removed — run --exec "VACUUM;" yourself
    # afterward if you want to reclaim space.
    python inspector.py --wipe-album 7
    python inspector.py --wipe-album 12,13,14
    python inspector.py --wipe-album 7-9

    # True factory reset: drops every table, INCLUDING system_config, so
    # tuned settings revert to defaults too. Nothing to run afterward —
    # the next DatabaseManager() (e.g. next time you scrape a new album)
    # calls _init_db() in __init__ and rebuilds schema + seeded config
    # automatically. Confirmation requires typing 'nuke', not 'yes'.
    python inspector.py --nuke

    # -y skips the confirmation prompt on --exec / --wipe / --nuke, for
    # scripted/non-interactive use (e.g. wiring a "reset" button to this later).
    python inspector.py --wipe -y
"""
import argparse
import sqlite3
import sys
import time
from contextlib import closing

from core import DatabaseManager
from rich.console import Console
from rich.panel import Panel
from rich.pretty import pprint
from rich.table import Table
from utils import format_bytes

console = Console()

DEFAULT_LIMIT = 5


# ============================================================
# Table introspection helpers
# ============================================================

def get_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
    ).fetchall()
    return [r["name"] for r in rows]


def get_row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) as c FROM {table};").fetchone()["c"]


def get_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({table});").fetchall()


# ============================================================
# Read-only reports (dashboard, per-album, expiring, staged)
# ============================================================

def display_global_dashboard(conn: sqlite3.Connection):
    """
    Scannable overview: every table, row count, and per-table pipeline
    metrics. Triggers automatically when inspector.py is run with zero
    args — see main() below.

    Each metric is queried in its OWN try/except so a single missing
    column (e.g. is_staged, before its migration has been run) only
    blanks that one number instead of discarding the whole row's metrics.
    An earlier version wrapped all of a table's metrics in one shared
    try block, which meant one failing query threw away metrics that had
    already been successfully computed just above it.
    """
    tables = get_table_names(conn)

    summary_table = Table(title="[bold magenta]Database Volume & Pipeline Summary[/bold magenta]", expand=True)
    summary_table.add_column("Table Name", style="cyan", no_wrap=True)
    summary_table.add_column("Total Records", style="magenta", justify="right")
    summary_table.add_column("Pipeline Execution Metrics / Breakdown", style="green")

    def safe_count(sql: str, fallback: str = "?") -> str:
        try:
            return str(conn.execute(sql).fetchone()["c"])
        except sqlite3.OperationalError:
            return fallback

    for table in tables:
        count = get_row_count(conn, table)
        metrics = "N/A"

        if table == "assets":
            comp = safe_count("SELECT COUNT(*) as c FROM assets WHERE download_status='COMPLETED';")
            fail = safe_count("SELECT COUNT(*) as c FROM assets WHERE download_status='FAILED';")
            pend = safe_count("SELECT COUNT(*) as c FROM assets WHERE download_status='PENDING';")
            staged = safe_count("SELECT COUNT(*) as c FROM assets WHERE is_staged=1;", fallback="[dim]n/a[/dim]")
            metrics = (
                f"[green]Completed: {comp}[/green] | [red]Failed: {fail}[/red] | "
                f"[yellow]Pending: {pend}[/yellow] | [cyan]Staged: {staged}[/cyan]"
            )
        elif table == "albums":
            staged_albums = safe_count("SELECT COUNT(*) as c FROM albums WHERE is_staged=1;", fallback="[dim]n/a[/dim]")
            metrics = f"[cyan]Staged Albums: {staged_albums}[/cyan]"
        elif table == "system_config":
            try:
                kvs = conn.execute("SELECT config_key, config_value FROM system_config;").fetchall()
                metrics = ", ".join([f"{r['config_key']}={r['config_value']}" for r in kvs])
            except sqlite3.OperationalError:
                pass

        summary_table.add_row(table, str(count), metrics)

    console.print(Panel(summary_table, border_style="magenta", title="[bold white]Media Tracker Management System[/bold white]"))


def display_albums_breakdown(conn: sqlite3.Connection):
    """
    One row per album: file count, download completion %, staged status,
    and total size. The global dashboard tells you totals across the
    whole DB; this tells you which specific albums still need attention.
    """
    albums = conn.execute("SELECT * FROM albums ORDER BY id ASC;").fetchall()
    if not albums:
        console.print("[bold yellow][!][/bold yellow] No albums in the database.")
        return

    def safe_scalar(sql: str, params: tuple, fallback):
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row and row[0] is not None else fallback
        except sqlite3.OperationalError:
            return fallback

    t = Table(title="[bold magenta]Per-Album Breakdown[/bold magenta]", expand=True)
    t.add_column("ID", justify="right", style="cyan")
    t.add_column("Title", style="white")
    t.add_column("Files", justify="right")
    t.add_column("Completed %", justify="right")
    t.add_column("Failed", justify="right", style="red")
    t.add_column("Staged", justify="right", style="green")
    t.add_column("Size", justify="right", style="magenta")

    for a in albums:
        total = safe_scalar("SELECT COUNT(*) FROM assets WHERE album_id = ?;", (a["id"],), 0)
        comp = safe_scalar("SELECT COUNT(*) FROM assets WHERE album_id = ? AND download_status='COMPLETED';", (a["id"],), 0)
        fail = safe_scalar("SELECT COUNT(*) FROM assets WHERE album_id = ? AND download_status='FAILED';", (a["id"],), 0)
        staged = safe_scalar("SELECT COUNT(*) FROM assets WHERE album_id = ? AND is_staged=1;", (a["id"],), "n/a")
        size_bytes = safe_scalar("SELECT SUM(raw_size_bytes) FROM assets WHERE album_id = ?;", (a["id"],), 0)

        pct = f"{(comp / total * 100):.0f}%" if total else "—"
        size_display = format_bytes(size_bytes) if isinstance(size_bytes, (int, float)) else size_bytes

        t.add_row(
            str(a["id"]), a["title"], str(total), pct,
            str(fail) if fail else "0",
            str(staged), size_display
        )

    console.print(t)


def display_album_detail(conn: sqlite3.Connection, album_id: int):
    """
    Everything about ONE album: its own record, plus every asset belonging
    to it, dumped in full (not truncated/tabulated — same reasoning as
    print_rows: long signed_cdn_url values need to stay readable and
    copy-paste intact, not wrapped or repr-quoted). Fills a real gap —
    --albums summarizes across every album, --table assets --all dumps
    every album's assets together with no way to isolate just one.
    """
    album = conn.execute("SELECT * FROM albums WHERE id = ?;", (album_id,)).fetchone()
    if not album:
        console.print(f"[bold red][-][/bold red] No album found with id {album_id}.")
        return

    console.print(f"[bold magenta]Album #{album['id']}[/bold magenta]")
    for key in album.keys():
        console.print(f"  [dim]{key}:[/dim] {album[key]}", soft_wrap=True, highlight=False)

    assets = conn.execute(
        "SELECT * FROM assets WHERE album_id = ? ORDER BY track_number ASC;", (album_id,)
    ).fetchall()

    if not assets:
        console.print(f"\n[dim](no assets for album {album_id})[/dim]")
        return

    console.print(f"\n[bold magenta]{len(assets)} asset(s):[/bold magenta]")
    for row in assets:
        console.print(f"[bold cyan]--- id={row['id']} ---[/bold cyan]")
        for key in row.keys():
            console.print(f"  [dim]{key}:[/dim] {row[key]}", soft_wrap=True, highlight=False)
        console.print()


def display_expiring_report(conn: sqlite3.Connection):
    """
    Which assets are stale or about to be, right now — independent of
    whether mint.py's daemon is actually running. Same lookahead window
    (token_buffer_seconds) get_needs_refresh() uses, so what shows up
    here is exactly what a mint pass would act on if you ran one.
    """
    buffer_row = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key='token_buffer_seconds';"
    ).fetchone()
    lookahead = int(buffer_row["config_value"]) if buffer_row else 600
    now = int(time.time())
    cutoff = now + lookahead

    rows = conn.execute("""
        SELECT assets.id, assets.title, assets.token_expiry_timestamp, albums.title as album_title
        FROM assets
        LEFT JOIN albums ON assets.album_id = albums.id
        WHERE assets.token_expiry_timestamp IS NULL
           OR assets.token_expiry_timestamp <= ?
        ORDER BY assets.token_expiry_timestamp ASC NULLS FIRST;
    """, (cutoff,)).fetchall()

    if not rows:
        console.print(f"[bold green][+][/bold green] Nothing expiring within the next {lookahead}s. All tokens fresh.")
        return

    t = Table(title=f"[bold magenta]Expiring/Expired Tokens (within {lookahead}s lookahead)[/bold magenta]", expand=True)
    t.add_column("Asset ID", justify="right", style="cyan")
    t.add_column("Album", style="white")
    t.add_column("Title", style="white")
    t.add_column("Status", style="yellow")

    expired_count = 0
    no_token_count = 0

    for r in rows:
        expiry = r["token_expiry_timestamp"]
        if expiry is None:
            status = "[dim white]No token[/dim white]"
            no_token_count += 1
        elif expiry <= now:
            status = "[bold red]Expired[/bold red]"
            expired_count += 1
        else:
            remaining = expiry - now
            mins = remaining // 60
            status = f"[yellow]Expiring in {mins}m[/yellow]"

        t.add_row(str(r["id"]), r["album_title"] or "—", r["title"], status)

    console.print(t)
    console.print(f"[dim]{len(rows)} total — {expired_count} expired, {no_token_count} never minted, "
                   f"{len(rows) - expired_count - no_token_count} expiring soon.[/dim]")


def display_staged_overview(conn: sqlite3.Connection):
    """
    High-level glance at staging state — grouped by album, not a per-row
    dump. A dump of every staged asset individually stops being useful
    once you have more than a handful staged at once; what's actually
    actionable is "how much is staged, where, and what state is it in."
    """
    def safe_rows(sql: str, fallback_msg: str):
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            console.print(f"[dim]{fallback_msg}[/dim]")
            return None

    staged_albums = safe_rows(
        "SELECT id, title, file_count FROM albums WHERE is_staged=1 ORDER BY id ASC;",
        "albums.is_staged not found — run: python inspector.py --add-column albums:is_staged:INTEGER"
    )
    staged_by_album = safe_rows(
        """SELECT a.album_id, al.title AS album_title,
                  COUNT(*) AS staged_count,
                  SUM(CASE WHEN a.download_status='COMPLETED' THEN 1 ELSE 0 END) AS comp,
                  SUM(CASE WHEN a.download_status='FAILED' THEN 1 ELSE 0 END) AS fail,
                  SUM(CASE WHEN a.download_status='PENDING' THEN 1 ELSE 0 END) AS pend,
                  SUM(a.raw_size_bytes) AS total_size
           FROM assets a
           LEFT JOIN albums al ON a.album_id = al.id
           WHERE a.is_staged = 1
           GROUP BY a.album_id
           ORDER BY al.title ASC;""",
        "assets.is_staged not found — run: python inspector.py --add-column assets:is_staged:INTEGER"
    )

    if staged_albums:
        t = Table(title="[bold magenta]Staged Albums (album-level flag)[/bold magenta]", expand=True)
        t.add_column("ID", justify="right", style="cyan")
        t.add_column("Title", style="white")
        t.add_column("Files", justify="right")
        for a in staged_albums:
            t.add_row(str(a["id"]), a["title"], str(a["file_count"]))
        console.print(t)
    elif staged_albums is not None:
        console.print("[dim]No albums flagged staged at the album level.[/dim]")

    if staged_by_album:
        t = Table(title="[bold magenta]Staged Assets by Album[/bold magenta]", expand=True)
        t.add_column("Album", style="white")
        t.add_column("Staged Files", justify="right", style="cyan")
        t.add_column("Completed", justify="right", style="green")
        t.add_column("Failed", justify="right", style="red")
        t.add_column("Pending", justify="right", style="yellow")
        t.add_column("Size", justify="right", style="magenta")

        total_staged = total_comp = total_fail = total_pend = 0
        total_size = 0
        for row in staged_by_album:
            t.add_row(
                row["album_title"] or "—",
                str(row["staged_count"]),
                str(row["comp"]),
                str(row["fail"]),
                str(row["pend"]),
                format_bytes(row["total_size"] or 0)
            )
            total_staged += row["staged_count"]
            total_comp += row["comp"]
            total_fail += row["fail"]
            total_pend += row["pend"]
            total_size += row["total_size"] or 0

        console.print(t)
        console.print(
            f"[bold]Totals:[/bold] {total_staged} staged file(s) across {len(staged_by_album)} album(s) — "
            f"[green]{total_comp} completed[/green], [red]{total_fail} failed[/red], "
            f"[yellow]{total_pend} pending[/yellow] — {format_bytes(total_size)}"
        )
    elif staged_by_album is not None:
        console.print("[dim]No staged assets.[/dim]")

    if staged_albums is not None and staged_by_album is not None and not staged_albums and not staged_by_album:
        console.print("[bold yellow][!][/bold yellow] Nothing is currently staged.")


# ============================================================
# Generic table dump + raw SQL (--table, --sql)
# ============================================================

def print_schema(conn: sqlite3.Connection, table: str):
    cols = get_columns(conn, table)
    t = Table(title=f"schema: {table}", show_lines=False)
    t.add_column("col")
    t.add_column("type")
    t.add_column("notnull")
    t.add_column("pk")
    for c in cols:
        t.add_row(c["name"], c["type"], str(bool(c["notnull"])), str(bool(c["pk"])))
    console.print(t)


def print_rows(conn: sqlite3.Connection, table: str, limit: int | None):
    """
    Plain flat dump — deliberately NOT using rich.pretty.pprint here.
    pprint repr-quotes strings and soft-wraps at terminal width, which
    mangles long values like signed_cdn_url (full query string, token,
    ex= param) across multiple lines. This prints each field on its own
    line with soft_wrap=True so Rich never inserts an artificial line
    break — the terminal just lets it run long, exactly as stored.
    """
    query = f"SELECT * FROM {table}"
    if limit is not None:
        query += f" LIMIT {limit}"
    query += ";"
    rows = conn.execute(query).fetchall()

    if not rows:
        console.print(f"[dim]  (no rows in {table})[/dim]")
        return

    for row in rows:
        console.print(f"[bold cyan]--- id={row['id']} ---[/bold cyan]")
        for key in row.keys():
            console.print(f"  [dim]{key}:[/dim] {row[key]}", soft_wrap=True, highlight=False)
        console.print()


def inspect_table(conn: sqlite3.Connection, table: str, limit: int | None):
    count = get_row_count(conn, table)
    console.print(f"\n[bold yellow][*] {table}[/bold yellow]  [dim]({count} rows)[/dim]")
    print_schema(conn, table)
    shown = "all" if limit is None else limit
    console.print(f"[bold white]  sample rows (showing {shown}):[/bold white]")
    print_rows(conn, table, limit)


def run_raw_sql(conn: sqlite3.Connection, sql: str):
    console.print(f"[bold yellow][*] Running:[/bold yellow] {sql}")
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as e:
        console.print(f"[bold red][x] Query error:[/bold red] {e}")
        return
    if not rows:
        console.print("[dim](no rows returned)[/dim]")
        return
    for row in rows:
        pprint(dict(row))


# ============================================================
# Schema migrations (--add-column, --drop-column)
# ============================================================

def add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
    """
    Adds a column to an existing table. Mirrors the old standalone
    upgrade_db.py pattern: SQLite has no 'ADD COLUMN IF NOT EXISTS', so we
    lean on the OperationalError it raises when the column already exists.
    """
    sql = f"ALTER TABLE {table} ADD COLUMN {column} {col_type};"
    console.print(f"[bold yellow][*] Running:[/bold yellow] {sql}")
    try:
        with conn:
            conn.execute(sql)
        console.print(f"[bold green][+][/bold green] Added '{column}' ({col_type}) to '{table}'.")
    except sqlite3.OperationalError as e:
        console.print(f"[bold yellow][!][/bold yellow] Skipped — '{e}' (column likely already exists).")


def drop_column(conn: sqlite3.Connection, table: str, column: str, confirm: bool = True):
    """
    Drops a column from an existing table. Requires SQLite 3.35+ for native
    ALTER TABLE ... DROP COLUMN support (bundled with Python 3.9+, so this
    should just work). Destructive — data in that column is gone for good,
    so it's confirmed like --exec/--wipe/--nuke rather than running silently.
    """
    if confirm:
        console.print(f"[bold red][!] This will permanently DROP column '{column}' from '{table}'. Data in it is lost.[/bold red]")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            console.print("[dim]Aborted.[/dim]")
            return

    sql = f"ALTER TABLE {table} DROP COLUMN {column};"
    console.print(f"[bold yellow][*] Running:[/bold yellow] {sql}")
    try:
        with conn:
            conn.execute(sql)
        console.print(f"[bold green][+][/bold green] Dropped '{column}' from '{table}'.")
    except sqlite3.OperationalError as e:
        console.print(f"[bold red][x] Failed:[/bold red] {e}")
        console.print("[dim]Common causes: column doesn't exist, is part of a PRIMARY KEY/UNIQUE index, "
                       "or your SQLite build predates 3.35 (DROP COLUMN support).[/dim]")


# ============================================================
# Selection parsing + staging (--stage-*, --unstage-*)
# ============================================================

def parse_id_selection(conn: sqlite3.Connection, table: str, selection: str) -> list[int]:
    """
    Parses the same selection syntax used elsewhere in the pipeline
    (download.py's '5 | 3,7,12 | 1-10' prompts), plus 'all':
        "14"        -> [14]
        "14,15,22"  -> [14, 15, 22]
        "1-10"      -> [1, 2, ..., 10]
        "1-5,8,10-12" -> mixed ranges and singles
        "all"       -> every id currently in `table`
    Raises ValueError with a clear message on malformed input.
    """
    selection = selection.strip().lower()
    if selection == "all":
        rows = conn.execute(f"SELECT id FROM {table};").fetchall()
        return [r["id"] for r in rows]

    ids: list[int] = []
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, _, end_s = part.partition("-")
            start, end = int(start_s.strip()), int(end_s.strip())
            if start > end:
                start, end = end, start
            ids.extend(range(start, end + 1))
        else:
            ids.append(int(part))
    return ids


def parse_asset_selection(conn: sqlite3.Connection, selection: str) -> list[int]:
    """Backward-compatible wrapper — assets was the only table this supported before."""
    return parse_id_selection(conn, "assets", selection)


def set_staged(conn: sqlite3.Connection, table: str, ids: list[int], staged: bool):
    """
    Toggles is_staged for one or more rows in albums/assets. Wraps the same
    UPDATE ... SET is_staged=... WHERE id IN (...) pattern, just without
    needing to hand-write/quote raw SQL each time. Reversible, so unlike
    --wipe/--nuke this doesn't prompt for confirmation.
    """
    placeholders = ", ".join("?" for _ in ids)
    sql = f"UPDATE {table} SET is_staged = ? WHERE id IN ({placeholders});"
    try:
        with conn:
            cursor = conn.execute(sql, (1 if staged else 0, *ids))
        verb = "Staged" if staged else "Unstaged"
        console.print(f"[bold green][+][/bold green] {verb} {cursor.rowcount} row(s) in '{table}'.")
    except sqlite3.OperationalError as e:
        console.print(f"[bold red][x] Failed:[/bold red] {e}")
        console.print(f"[dim]Has is_staged been added to '{table}' yet? "
                       f"python inspector.py --add-column {table}:is_staged:INTEGER[/dim]")


def stage_album_cascade(conn: sqlite3.Connection, album_id: int, staged: bool):
    """
    Stages/unstages an album AND cascades the same value to every asset
    belonging to it, in one transaction. This is the intended behavior:
    staging an album means staging its contents, not just flagging the
    album record on its own.
    """
    try:
        with conn:
            album_cursor = conn.execute(
                "UPDATE albums SET is_staged = ? WHERE id = ?;", (1 if staged else 0, album_id)
            )
            asset_cursor = conn.execute(
                "UPDATE assets SET is_staged = ? WHERE album_id = ?;", (1 if staged else 0, album_id)
            )
        verb = "Staged" if staged else "Unstaged"
        if album_cursor.rowcount == 0:
            console.print(f"[bold yellow][!][/bold yellow] No album found with id {album_id} — nothing changed.")
            return
        console.print(f"[bold green][+][/bold green] {verb} album #{album_id} "
                       f"and cascaded to {asset_cursor.rowcount} asset(s).")
    except sqlite3.OperationalError as e:
        console.print(f"[bold red][x] Failed:[/bold red] {e}")
        console.print("[dim]Has is_staged been added to both 'albums' and 'assets' yet? "
                       "python inspector.py --add-column albums:is_staged:INTEGER  |  "
                       "python inspector.py --add-column assets:is_staged:INTEGER[/dim]")


# ============================================================
# Write / destructive operations (--exec, --wipe*, --nuke)
# ============================================================

def run_exec_sql(conn: sqlite3.Connection, sql: str, confirm: bool = True):
    """
    For write statements (DELETE, UPDATE, VACUUM, etc). Commits explicitly,
    since these don't return rows the way run_raw_sql's SELECT path expects.
    """
    if confirm:
        console.print(f"[bold red][!] About to execute (this writes to the DB):[/bold red] {sql}")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            console.print("[dim]Aborted.[/dim]")
            return

    try:
        with conn:
            cursor = conn.execute(sql)
        console.print(f"[bold green][+][/bold green] Done. {cursor.rowcount if cursor.rowcount != -1 else ''} row(s) affected.")
    except Exception as e:
        console.print(f"[bold red][x] Exec error:[/bold red] {e}")


def wipe_album(conn: sqlite3.Connection, album_ids: list[int], confirm: bool = True):
    """
    Deletes one or more albums and their assets, leaving every other album
    untouched. Multiple ids share ONE confirmation prompt (listing each
    album found) and run in ONE transaction — not a separate confirm per
    album, which would be tedious for something like --wipe-album 12,13,14.
    No VACUUM here (unlike wipe_data/nuke_db) — VACUUM's cost scales with
    the whole DB file, not the rows removed, so running it on every
    wipe would be wasteful if you're doing this repeatedly.
    Run --exec "VACUUM;" yourself afterward if you want to reclaim space.
    """
    found = []
    missing = []
    for album_id in album_ids:
        row = conn.execute("SELECT id, title FROM albums WHERE id = ?;", (album_id,)).fetchone()
        if row:
            asset_count = conn.execute("SELECT COUNT(*) as c FROM assets WHERE album_id = ?;", (album_id,)).fetchone()["c"]
            found.append((row["id"], row["title"], asset_count))
        else:
            missing.append(album_id)

    if missing:
        console.print(f"[bold yellow][!][/bold yellow] No album found for id(s): {', '.join(str(m) for m in missing)} — skipping those.")

    if not found:
        console.print("[bold yellow][!][/bold yellow] Nothing to wipe.")
        return

    if confirm:
        console.print("[bold red][!] This will DELETE the following album(s) and their assets. Other albums are untouched:[/bold red]")
        for aid, title, acount in found:
            console.print(f"    #{aid} ('{title}') — {acount} asset(s)")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            console.print("[dim]Aborted.[/dim]")
            return

    with conn:
        for aid, _, _ in found:
            conn.execute("DELETE FROM assets WHERE album_id = ?;", (aid,))
            conn.execute("DELETE FROM albums WHERE id = ?;", (aid,))

    total_assets = sum(a[2] for a in found)
    console.print(f"[bold green][+][/bold green] Wiped {len(found)} album(s) and {total_assets} asset(s): "
                   f"{', '.join(f'#{a[0]}' for a in found)}.")


def wipe_data(conn: sqlite3.Connection, confirm: bool = True):
    """
    Soft wipe: clears albums + assets (cascades via FK), keeps system_config
    intact, then reclaims disk space. Use for 'start fresh' without losing
    tuned settings like poll intervals.
    """
    if confirm:
        console.print("[bold red][!] This will DELETE all albums and assets. system_config is kept.[/bold red]")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            console.print("[dim]Aborted.[/dim]")
            return

    with conn:
        conn.execute("DELETE FROM assets;")
        conn.execute("DELETE FROM albums;")
    conn.execute("VACUUM;")
    console.print("[bold green][+][/bold green] Wiped albums + assets. system_config preserved.")


def nuke_db(conn: sqlite3.Connection, confirm: bool = True):
    """
    True factory reset: drops every table, including system_config, so
    tuned settings (poll intervals, max_workers, etc) revert to defaults too.
    Nothing needs to be run afterward — the next DatabaseManager() instance
    (e.g. the next time reader.py ingests a scrape) calls _init_db() in its
    __init__, which recreates tables/indexes/seeded config via
    CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE. Rebuild is automatic.
    """
    if confirm:
        console.print("[bold red][!] This will DROP ALL TABLES, including system_config (settings reset to defaults).[/bold red]")
        answer = input("Type 'nuke' to continue: ").strip().lower()
        if answer != "nuke":
            console.print("[dim]Aborted.[/dim]")
            return

    with conn:
        tables = get_table_names(conn)
        for table in tables:
            conn.execute(f"DROP TABLE IF EXISTS {table};")
    conn.execute("VACUUM;")
    console.print(f"[bold green][+][/bold green] Dropped {len(tables)} table(s): {', '.join(tables)}.")
    console.print("[dim]Schema + default config will auto-rebuild next time anything opens this DB (e.g. next scrape).[/dim]")


# ============================================================
# CLI entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Inspect and manage media_tracker.db")

    view = parser.add_argument_group("Views (read-only)")
    view.add_argument("--table", help="Only inspect this table")
    view.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT,
                       help=f"Number of sample rows to show (default {DEFAULT_LIMIT})")
    view.add_argument("--all", action="store_true", help="Show every row, ignore --limit")
    view.add_argument("--sql", help="Run an arbitrary read query instead of the standard dump")
    view.add_argument("--albums", action="store_true",
                       help="Per-album breakdown: completion %%, staged count, size, per album")
    view.add_argument("--expiring", action="store_true",
                       help="List assets expiring/expired right now, independent of mint.py running")
    view.add_argument("--staged", action="store_true",
                       help="High-level staging summary, grouped by album")
    view.add_argument("--db-id", type=int, metavar="ID",
                       help="Show one album's full record + all its assets, untruncated (fills the gap "
                            "between --albums summarizing everything and --table dumping every album's assets together)")

    migrate = parser.add_argument_group("Schema migrations")
    migrate.add_argument("--add-column", dest="add_column",
                          help="Add a column: table:column:type, e.g. assets:true_file_id:INTEGER")
    migrate.add_argument("--drop-column", dest="drop_column",
                          help="Drop a column: table:column, e.g. assets:true_file_id")

    stage = parser.add_argument_group("Staging (--stage-*, cascading for albums)")
    stage.add_argument("--stage-album", type=int, metavar="ID",
                        help="Set is_staged=1 for an album AND cascade to all its assets")
    stage.add_argument("--unstage-album", type=int, metavar="ID",
                        help="Set is_staged=0 for an album AND cascade to all its assets")
    stage.add_argument("--stage-assets", metavar="SELECTION",
                        help="Set is_staged=1 for assets: id, comma list, range, or 'all' — e.g. 14,15,22 or 1-10 or all")
    stage.add_argument("--unstage-assets", metavar="SELECTION",
                        help="Set is_staged=0 for assets: id, comma list, range, or 'all' — e.g. 14,15,22 or 1-10 or all")

    write = parser.add_argument_group("Write / destructive operations")
    write.add_argument("--exec", dest="exec_sql",
                        help="Run an arbitrary WRITE statement (DELETE/UPDATE/VACUUM/etc), with confirmation")
    write.add_argument("--wipe", action="store_true",
                        help="Clear albums + assets, keep system_config, reclaim disk space ('start fresh')")
    write.add_argument("--wipe-album", metavar="SELECTION",
                        help="Delete one or more albums + their assets: id, comma list, or range — e.g. 12,13,14 or 7-9. "
                             "Use --wipe (not 'all' here) to clear every album.")
    write.add_argument("--nuke", "--purge", dest="nuke", action="store_true",
                        help="Drop ALL tables including system_config (true factory reset; auto-rebuilds on next use)")
    write.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt for --exec / --wipe / --nuke (for scripting)")

    args = parser.parse_args()

    db = DatabaseManager()  # reuses the same db_path/schema as the rest of the app

    with closing(db._get_connection()) as conn:
        if args.nuke:
            nuke_db(conn, confirm=not args.yes)
            return

        if args.wipe:
            wipe_data(conn, confirm=not args.yes)
            return

        if args.wipe_album is not None:
            if args.wipe_album.strip().lower() == "all":
                console.print("[bold yellow][!][/bold yellow] --wipe-album doesn't take 'all' — use --wipe instead "
                               "(it also clears system_config-independent data the same way).")
                return
            try:
                album_ids = parse_id_selection(conn, "albums", args.wipe_album)
            except ValueError:
                console.print("[bold red][x] --wipe-album expects an id, comma list, or range, e.g. 12,13,14 or 7-9[/bold red]")
                return
            wipe_album(conn, album_ids, confirm=not args.yes)
            return

        if args.exec_sql:
            run_exec_sql(conn, args.exec_sql, confirm=not args.yes)
            return

        if args.add_column:
            try:
                table, column, col_type = args.add_column.split(":")
            except ValueError:
                console.print("[bold red][x] --add-column expects format table:column:type, e.g. assets:true_file_id:INTEGER[/bold red]")
                return
            add_column(conn, table, column, col_type)
            return

        if args.drop_column:
            try:
                table, column = args.drop_column.split(":")
            except ValueError:
                console.print("[bold red][x] --drop-column expects format table:column, e.g. assets:true_file_id[/bold red]")
                return
            drop_column(conn, table, column, confirm=not args.yes)
            return

        if args.stage_album is not None:
            stage_album_cascade(conn, args.stage_album, staged=True)
            return

        if args.unstage_album is not None:
            stage_album_cascade(conn, args.unstage_album, staged=False)
            return

        if args.stage_assets:
            try:
                ids = parse_asset_selection(conn, args.stage_assets)
            except ValueError:
                console.print("[bold red][x] --stage-assets expects an id, comma list, range, or 'all', e.g. 14,15,22 or 1-10 or all[/bold red]")
                return
            if not ids:
                console.print("[bold yellow][!][/bold yellow] No matching asset ids — nothing to stage.")
                return
            set_staged(conn, "assets", ids, staged=True)
            return

        if args.unstage_assets:
            try:
                ids = parse_asset_selection(conn, args.unstage_assets)
            except ValueError:
                console.print("[bold red][x] --unstage-assets expects an id, comma list, range, or 'all', e.g. 14,15,22 or 1-10 or all[/bold red]")
                return
            if not ids:
                console.print("[bold yellow][!][/bold yellow] No matching asset ids — nothing to unstage.")
                return
            set_staged(conn, "assets", ids, staged=False)
            return

        if args.sql:
            run_raw_sql(conn, args.sql)
            return

        if args.albums:
            display_albums_breakdown(conn)
            return

        if args.expiring:
            display_expiring_report(conn)
            return

        if args.staged:
            display_staged_overview(conn)
            return

        if args.db_id is not None:
            display_album_detail(conn, args.db_id)
            return

        # Overview dashboard when run with truly zero args. The len(sys.argv)
        # check (not just "not args.table and not args.all") matters here:
        # someone running `-n 5` alone should still get the full table dump
        # at that limit, not get redirected into the dashboard.
        if not args.table and not args.all and len(sys.argv) == 1:
            display_global_dashboard(conn)
            return

        limit = None if args.all else args.limit

        if args.table:
            inspect_table(conn, args.table, limit)
            return

        tables = get_table_names(conn)
        if not tables:
            console.print("[bold red][!] No tables found in this database.[/bold red]")
            return

        console.print(f"[bold green][+][/bold green] Found {len(tables)} table(s): {', '.join(tables)}")
        for table in tables:
            inspect_table(conn, table, limit)


if __name__ == "__main__":
    main()