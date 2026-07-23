# inspector.py
"""
Quick CLI to poke at media_tracker.db without writing one-off queries.

Usage:
    python inspector.py                     # zero args: dashboard overview
    python inspector.py --table assets       # just one table
    python inspector.py --table assets -n 20 # show 20 sample rows instead of default 5
    python inspector.py --table assets --all # dump every row
    python inspector.py --sql "SELECT * FROM assets WHERE download_status='FAILED'"
    python inspector.py --albums            # per-album breakdown
    python inspector.py --expiring          # tokens expiring soon
    python inspector.py --staged            # staging overview
    python inspector.py --stage-album 2      # stage album 2 + cascaded assets
    python inspector.py --unstage-album 2
    python inspector.py --stage-assets 1-10
    python inspector.py --unstage-assets all
    python inspector.py --exec "DELETE FROM assets WHERE download_status='FAILED';"
    python inspector.py --add-column assets:true_file_id:INTEGER
    python inspector.py --drop-column assets:true_file_id
    python inspector.py --wipe
    python inspector.py --wipe-album 7-9
    python inspector.py --nuke
    python inspector.py --wipe -y
"""
import argparse
import sqlite3
import sys
import time
from contextlib import closing

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from core import DatabaseManager
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
    """Scannable overview: every table, row count, and per-table pipeline metrics."""
    tables = get_table_names(conn)

    summary_table = Table(
        title="[bold magenta]Database Volume & Pipeline Summary[/bold magenta]",
        expand=True,
        style="dim white",
        border_style="dim"
    )
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

    console.print(Panel(summary_table, border_style="dim", title="[bold white]Media Tracker Management System[/bold white]"))


def display_albums_breakdown(conn: sqlite3.Connection):
    """One row per album: file count, download completion %, staged status, and total size."""
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

    t = Table(
        title="[bold magenta]Per-Album Breakdown[/bold magenta]",
        expand=True,
        style="dim white",
        border_style="dim"
    )
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


def display_expiring_report(conn: sqlite3.Connection):
    """Which assets are stale or about to be right now."""
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

    t = Table(
        title=f"[bold magenta]Expiring/Expired Tokens (within {lookahead}s lookahead)[/bold magenta]",
        expand=True,
        style="dim white",
        border_style="dim"
    )
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
    """High-level glance at staging state — grouped by album."""
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
        t = Table(
            title="[bold magenta]Staged Albums (album-level flag)[/bold magenta]",
            expand=True,
            style="dim white",
            border_style="dim"
        )
        t.add_column("ID", justify="right", style="cyan")
        t.add_column("Title", style="white")
        t.add_column("Files", justify="right")
        for a in staged_albums:
            t.add_row(str(a["id"]), a["title"], str(a["file_count"]))
        console.print(t)
    elif staged_albums is not None:
        console.print("[dim]No albums flagged staged at the album level.[/dim]")

    if staged_by_album:
        t = Table(
            title="[bold magenta]Staged Assets by Album[/bold magenta]",
            expand=True,
            style="dim white",
            border_style="dim"
        )
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
    """Renders table schema structure using a formatted Rich Table."""
    cols = get_columns(conn, table)
    t = Table(
        title=f"Schema Definition: [cyan]{table}[/cyan]",
        show_header=True,
        header_style="bold magenta",
        style="dim white",
        border_style="dim"
    )
    t.add_column("Column", style="white")
    t.add_column("Type", style="yellow")
    t.add_column("Not Null", justify="center")
    t.add_column("PK", justify="center", style="magenta")
    
    for c in cols:
        is_nn = "[bold green]YES[/bold green]" if c["notnull"] else "[dim white]NO[/dim white]"
        is_pk = "[bold green]YES[/bold green]" if c["pk"] else "[dim white]NO[/dim white]"
        t.add_row(c["name"], c["type"], is_nn, is_pk)
    console.print(t)


def format_cell_value(col_name: str, val) -> str:
    """Helper to format column data smartly according to app context."""
    if val is None:
        return "[dim white]NULL[/dim white]"
    
    val_str = str(val)
    
    # Specific styling logic based on column context
    if col_name == "download_status":
        if val == "COMPLETED": return "[bold green]COMPLETED[/bold green]"
        if val == "FAILED": return "[bold red]FAILED[/bold red]"
        if val == "PENDING": return "[bold yellow]PENDING[/bold yellow]"
    elif col_name == "is_staged":
        return "[bold green]1 (STAGED)[/bold green]" if val == 1 else "[dim white]0[/dim white]"
    elif col_name == "raw_size_bytes":
        return format_bytes(val)
    elif "url" in col_name and len(val_str) > 40:
        return f"[blue]{val_str[:37]}...[/blue]"
        
    return val_str


def print_rows(conn: sqlite3.Connection, table: str, limit: int | None):
    """Renders query results in a clean grid instead of raw dictionaries."""
    query = f"SELECT * FROM {table}"
    if limit is not None:
        query += f" LIMIT {limit}"
    query += ";"
    rows = conn.execute(query).fetchall()

    if not rows:
        console.print(f"[dim]  (no records discovered inside '{table}')[/dim]")
        return

    cols = rows[0].keys()
    
    t = Table(
        show_header=True,
        header_style="bold cyan",
        style="dim white",
        border_style="dim",
        expand=True
    )
    for col in cols:
        t.add_column(col)

    for row in rows:
        row_dict = dict(row)
        formatted_row = [format_cell_value(k, row_dict[k]) for k in cols]
        t.add_row(*formatted_row)

    console.print(t)


def inspect_table(conn: sqlite3.Connection, table: str, limit: int | None):
    count = get_row_count(conn, table)
    console.print(f"\n[bold yellow][*] Inspecting Table:[/bold yellow] [bold white]{table}[/bold white] [dim]({count} total records)[/dim]")
    print_schema(conn, table)
    shown = "all" if limit is None else limit
    console.print(f"\n[bold cyan]  Dataset Sample Records (showing {shown}):[/bold cyan]")
    print_rows(conn, table, limit)


def run_raw_sql(conn: sqlite3.Connection, sql: str):
    console.print(f"[bold yellow][*] Executing Query Sequence:[/bold yellow] {sql}")
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as e:
        console.print(f"[bold red][x] Query Execution Error:[/bold red] {e}")
        return
        
    if not rows:
        console.print("[dim](query executed successfully, no rows returned)[/dim]")
        return

    cols = rows[0].keys()
    t = Table(
        show_header=True,
        header_style="bold cyan",
        style="dim white",
        border_style="dim",
        expand=True
    )
    for col in cols:
        t.add_column(col)

    for row in rows:
        row_dict = dict(row)
        formatted_row = [format_cell_value(k, row_dict[k]) for k in cols]
        t.add_row(*formatted_row)

    console.print(t)


# ============================================================
# Schema migrations (--add-column, --drop-column)
# ============================================================

def add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
    sql = f"ALTER TABLE {table} ADD COLUMN {column} {col_type};"
    console.print(f"[bold yellow][*] Running Migration:[/bold yellow] {sql}")
    try:
        with conn:
            conn.execute(sql)
        console.print(f"[bold green][+][/bold green] Added '{column}' ({col_type}) to '{table}'.")
    except sqlite3.OperationalError as e:
        console.print(f"[bold yellow][!][/bold yellow] Skipped — '{e}' (column likely already exists).")


def drop_column(conn: sqlite3.Connection, table: str, column: str, confirm: bool = True):
    if confirm:
        console.print(f"[bold red][!] This will permanently DROP column '{column}' from '{table}'. Data in it is lost.[/bold red]")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            console.print("[dim]Aborted.[/dim]")
            return

    sql = f"ALTER TABLE {table} DROP COLUMN {column};"
    console.print(f"[bold yellow][*] Running Migration:[/bold yellow] {sql}")
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
    return parse_id_selection(conn, "assets", selection)


def set_staged(conn: sqlite3.Connection, table: str, ids: list[int], staged: bool):
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