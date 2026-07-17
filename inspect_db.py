# inspect_db.py
"""
Quick CLI to poke at media_tracker.db without writing one-off queries.

Usage:
    python inspect_db.py                     # overview: every table, counts, sample rows
    python inspect_db.py --table assets       # just one table
    python inspect_db.py --table assets -n 20 # show 20 sample rows instead of the default 5
    python inspect_db.py --table assets --all # dump every row (careful on big tables)
    python inspect_db.py --sql "SELECT * FROM assets WHERE download_status='FAILED'"

    # Write operations (DELETE/UPDATE/VACUUM/etc) — these commit for real,
    # unlike --sql above which is read-only (.fetchall(), never commits).
    python inspect_db.py --exec "DELETE FROM assets WHERE download_status='FAILED';"
    python inspect_db.py --exec "UPDATE assets SET token_expiry_timestamp = NULL;"

    # Schema migrations: add a column to an existing table. Replaces the old
    # standalone upgrade_db.py — safe to re-run, skips if column exists.
    python inspect_db.py --add-column assets:true_file_id:INTEGER

    # Opposite: drop a column. Requires SQLite 3.35+ (bundled with Python
    # 3.9+). Destructive — confirms by default, -y skips the prompt.
    python inspect_db.py --drop-column assets:true_file_id

    # "Start fresh": clears albums + assets (cascades via FK), keeps
    # system_config (poll intervals, max_workers, etc) intact, then VACUUMs
    # to reclaim disk space. Schema itself is untouched — DatabaseManager's
    # _init_db() would just recreate it anyway (CREATE TABLE IF NOT EXISTS),
    # so there's nothing to "rebuild" here, only data to clear.
    python inspect_db.py --wipe

    # True factory reset: drops every table, INCLUDING system_config, so
    # tuned settings revert to defaults too. Nothing to run afterward —
    # the next DatabaseManager() (e.g. next time you scrape a new album)
    # calls _init_db() in __init__ and rebuilds schema + seeded config
    # automatically. Confirmation requires typing 'nuke', not 'yes'.
    python inspect_db.py --nuke

    # -y skips the confirmation prompt on --exec / --wipe / --nuke, for
    # scripted/non-interactive use (e.g. wiring a "reset" button to this later).
    python inspect_db.py --wipe -y
"""
import argparse
import sqlite3
from contextlib import closing

from rich.console import Console
from rich.table import Table
from rich.pretty import pprint

from core import DatabaseManager

console = Console()

DEFAULT_LIMIT = 5


def get_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
    ).fetchall()
    return [r["name"] for r in rows]


def get_row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) as c FROM {table};").fetchone()["c"]


def get_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({table});").fetchall()


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
    query = f"SELECT * FROM {table}"
    if limit is not None:
        query += f" LIMIT {limit}"
    query += ";"
    rows = conn.execute(query).fetchall()

    if not rows:
        console.print(f"[dim]  (no rows in {table})[/dim]")
        return

    for row in rows:
        pprint(dict(row))


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


def main():
    parser = argparse.ArgumentParser(description="Inspect media_tracker.db")
    parser.add_argument("--table", help="Only inspect this table")
    parser.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT,
                         help=f"Number of sample rows to show (default {DEFAULT_LIMIT})")
    parser.add_argument("--all", action="store_true", help="Show every row, ignore --limit")
    parser.add_argument("--sql", help="Run an arbitrary read query instead of the standard dump")
    parser.add_argument("--exec", dest="exec_sql",
                         help="Run an arbitrary WRITE statement (DELETE/UPDATE/VACUUM/etc), with confirmation")
    parser.add_argument("--add-column", dest="add_column",
                         help="Add a column: table:column:type, e.g. assets:true_file_id:INTEGER")
    parser.add_argument("--drop-column", dest="drop_column",
                         help="Drop a column: table:column, e.g. assets:true_file_id")
    parser.add_argument("--wipe", action="store_true",
                         help="Clear albums + assets, keep system_config, reclaim disk space ('start fresh')")
    parser.add_argument("--nuke", "--purge", dest="nuke", action="store_true",
                         help="Drop ALL tables including system_config (true factory reset; auto-rebuilds on next use)")
    parser.add_argument("-y", "--yes", action="store_true",
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

        if args.sql:
            run_raw_sql(conn, args.sql)
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