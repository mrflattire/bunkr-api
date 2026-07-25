import argparse
import sqlite3
import sys
import time
from contextlib import closing
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

# Internal package imports
from .core.db import DatabaseManager
from .utils.formatting import format_bytes, parse_selection

console = Console()

class Inspector:
    def __init__(self):
        self.db = DatabaseManager()

    def get_conn(self):
        return self.db._get_connection()

    # ============================================================
    # READ-ONLY REPORTS
    # ============================================================

    def display_table(self, table_name, limit=10, search=None, show_all=False):
        """
        Generic flat dump for any table not covered by a named view
        (dashboard/albums/expiring/staged/album). Flat key:value lines,
        not a Table — a Rich Table here would repr/truncate long values
        like signed_cdn_url, the exact problem an earlier fix solved.
        """
        with closing(self.get_conn()) as conn:
            query = f"SELECT * FROM {table_name}"
            clauses, params = [], []
            if search:
                clauses.append("title LIKE ?")
                params.append(f"%{search}%")
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            if not show_all:
                query += " LIMIT ?"
                params.append(limit)
            query += ";"

            try:
                rows = conn.execute(query, params).fetchall()
            except sqlite3.OperationalError as e:
                console.print(f"[red]Query error: {e}[/red]")
                return

            if not rows:
                console.print(f"[yellow]No records found in {table_name}.[/yellow]")
                return

            console.print(f"[bold white]{len(rows)} row(s):[/bold white]")
            for row in rows:
                console.print(f"[bold cyan]--- id={row['id']} ---[/bold cyan]")
                for key in row.keys():
                    console.print(f"  [dim]{key}:[/dim] {row[key]}", soft_wrap=True, highlight=False)
                console.print()

    def display_dashboard(self):
        """Zero-args default: Global volume and metrics overview."""
        with closing(self.get_conn()) as conn:
            tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';").fetchall()]
            
            summary = Table(title="[bold magenta]Database Volume & Pipeline Summary[/bold magenta]", expand=True)
            summary.add_column("Table Name", style="cyan")
            summary.add_column("Total Records", justify="right", style="magenta")
            summary.add_column("Pipeline Execution Metrics", style="green")

            def safe_count(sql):
                try: return str(conn.execute(sql).fetchone()[0])
                except: return "?"

            for table in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                metrics = "N/A"
                
                if table == "assets":
                    c = safe_count("SELECT COUNT(*) FROM assets WHERE download_status='COMPLETED'")
                    f = safe_count("SELECT COUNT(*) FROM assets WHERE download_status='FAILED'")
                    s = safe_count("SELECT COUNT(*) FROM assets WHERE is_staged=1")
                    metrics = f"[green]Done: {c}[/green] | [red]Fail: {f}[/red] | [yellow]Staged: {s}[/yellow]"
                
                elif table == "albums":
                    s = safe_count("SELECT COUNT(*) FROM albums WHERE is_staged=1")
                    metrics = f"[yellow]Staged Albums: {s}[/yellow]"
                
                # RESTORED: System Config Metrics Logic
                elif table == "system_config":
                    try:
                        kvs = conn.execute("SELECT config_key, config_value FROM system_config;").fetchall()
                        metrics = ", ".join([f"[bold white]{r['config_key']}[/bold white]={r['config_value']}" for r in kvs])
                    except sqlite3.OperationalError:
                        pass
                
                summary.add_row(table, str(count), metrics)
            
            console.print(Panel(summary, border_style="magenta", title="[bold white]Media Tracker Management System[/bold white]"))

    def display_albums(self):
        """Per-album breakdown table."""
        with closing(self.get_conn()) as conn:
            rows = conn.execute("SELECT * FROM albums ORDER BY id ASC;").fetchall()
            if not rows:
                console.print("[yellow]No albums cataloged.[/yellow]")
                return

            t = Table(title="[bold magenta]Per-Album Breakdown[/bold magenta]", expand=True)
            t.add_column("ID", style="cyan", justify="right")
            t.add_column("Title")
            t.add_column("Files", justify="right")
            t.add_column("Status", justify="right")
            t.add_column("Staged", justify="right", style="green")
            t.add_column("Size", justify="right", style="magenta")

            for a in rows:
                # Query metrics for specific album
                stats = conn.execute("""
                    SELECT 
                        COUNT(*), 
                        SUM(CASE WHEN download_status='COMPLETED' THEN 1 ELSE 0 END), 
                        SUM(CASE WHEN download_status='FAILED' THEN 1 ELSE 0 END),
                        SUM(is_staged)
                    FROM assets WHERE album_id=?
                """, (a['id'],)).fetchone()
                
                total, comp, fail, staged = stats
                pct = f"{(comp/total*100):.0f}%" if total else "0%"
                
                # aggregate_size is stored as raw INTEGER bytes in core.py's
                # schema (populated straight from the scrape JSON), not
                # pre-formatted text — format_bytes() gives '1.20 GB'
                # instead of a raw byte count like '1288490188'.
                t.add_row(
                    str(a['id']), 
                    a['title'], 
                    str(total), 
                    f"{pct} [red](!{fail})[/red]" if fail else pct, 
                    str(staged or 0), 
                    format_bytes(a['aggregate_size'])
                )
            console.print(t)

    def display_album_detail(self, album_id):
        """Detailed dump of one album and its assets."""
        with closing(self.get_conn()) as conn:
            album = conn.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
            if not album:
                console.print(f"[red]Album {album_id} not found.[/red]")
                return
            
            console.print(f"[bold magenta]Album Detail: #{album['id']}[/bold magenta]")
            for k in album.keys():
                console.print(f"  [dim]{k}:[/dim] {album[k]}", soft_wrap=True)

            assets = conn.execute("SELECT * FROM assets WHERE album_id=? ORDER BY track_number ASC", (album_id,)).fetchall()
            for r in assets:
                console.print(f"\n[bold cyan]--- Asset ID: {r['id']} (Track {r['track_number']}) ---[/bold cyan]")
                for k in r.keys():
                    console.print(f"  [dim]{k}:[/dim] {r[k]}", soft_wrap=True, highlight=False)

    def display_expiring(self):
        """Report for assets requiring token renewal."""
        assets = self.db.get_needs_refresh()
        if not assets:
            console.print("[green]All tokens are fresh.[/green]")
            return
        
        t = Table(title="Expiring/Expired Tokens")
        t.add_column("ID", justify="right")
        t.add_column("Title")
        t.add_column("Status", style="yellow")
        for a in assets:
            expiry = a['token_expiry_timestamp']
            if not expiry:
                status = "[dim]No Token[/dim]"
            else:
                rem = expiry - int(time.time())
                status = f"Stale ({rem//60}m)" if rem > 0 else "[bold red]Expired[/bold red]"
            t.add_row(str(a['id']), a['title'], status)
        console.print(t)

    def display_staged(self):
        """
        Grouped-by-album staging summary — NOT the same as the dashboard's
        single aggregate 'Staged: N' count. This was previously aliased
        straight to display_dashboard(), which loses the per-album
        breakdown entirely.
        """
        with closing(self.get_conn()) as conn:
            staged_albums = conn.execute(
                "SELECT id, title, file_count FROM albums WHERE is_staged=1 ORDER BY id ASC;"
            ).fetchall()
            staged_by_album = conn.execute("""
                SELECT a.album_id, al.title AS album_title,
                       COUNT(*) AS staged_count,
                       SUM(CASE WHEN a.download_status='COMPLETED' THEN 1 ELSE 0 END) AS comp,
                       SUM(CASE WHEN a.download_status='FAILED' THEN 1 ELSE 0 END) AS fail,
                       SUM(a.raw_size_bytes) AS total_size
                FROM assets a
                LEFT JOIN albums al ON a.album_id = al.id
                WHERE a.is_staged = 1
                GROUP BY a.album_id
                ORDER BY al.title ASC;
            """).fetchall()

            if staged_albums:
                t = Table(title="[bold magenta]Staged Albums (album-level flag)[/bold magenta]", expand=True)
                t.add_column("ID", justify="right", style="cyan")
                t.add_column("Title")
                t.add_column("Files", justify="right")
                for a in staged_albums:
                    t.add_row(str(a["id"]), a["title"], str(a["file_count"]))
                console.print(t)
            else:
                console.print("[dim]No albums flagged staged at the album level.[/dim]")

            if staged_by_album:
                t = Table(title="[bold magenta]Staged Assets by Album[/bold magenta]", expand=True)
                t.add_column("Album")
                t.add_column("Staged Files", justify="right", style="cyan")
                t.add_column("Completed", justify="right", style="green")
                t.add_column("Failed", justify="right", style="red")
                t.add_column("Size", justify="right", style="magenta")
                total_staged = total_size = 0
                for row in staged_by_album:
                    t.add_row(row["album_title"] or "—", str(row["staged_count"]),
                              str(row["comp"]), str(row["fail"]), format_bytes(row["total_size"] or 0))
                    total_staged += row["staged_count"]
                    total_size += row["total_size"] or 0
                console.print(t)
                console.print(f"[bold]Totals:[/bold] {total_staged} staged file(s) across "
                              f"{len(staged_by_album)} album(s) — {format_bytes(total_size)}")
            else:
                console.print("[dim]No staged assets.[/dim]")

    # ============================================================
    # SCHEMA MIGRATIONS
    # ============================================================

    def add_column(self, spec: str):
        try:
            table, column, col_type = spec.split(":")
        except ValueError:
            console.print("[red]Expected table:column:type, e.g. assets:true_file_id:INTEGER[/red]")
            return
        with closing(self.get_conn()) as conn:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type};")
                conn.commit()
                console.print(f"[green]Added '{column}' ({col_type}) to '{table}'.[/green]")
            except sqlite3.OperationalError as e:
                console.print(f"[yellow]Skipped — {e} (likely already exists).[/yellow]")

    def drop_column(self, spec: str, force: bool = False):
        try:
            table, column = spec.split(":")
        except ValueError:
            console.print("[red]Expected table:column, e.g. assets:true_file_id[/red]")
            return
        if not force and not Confirm.ask(f"[bold red]Permanently DROP '{column}' from '{table}'?[/bold red]"):
            return
        with closing(self.get_conn()) as conn:
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column};")
                conn.commit()
                console.print(f"[green]Dropped '{column}' from '{table}'.[/green]")
            except sqlite3.OperationalError as e:
                console.print(f"[red]Failed: {e}[/red]")

    # ============================================================
    # WRITE OPERATIONS
    # ============================================================

    def _resolve_bound(self, conn, table):
        """Shared MAX(id) bound for parse_selection — a real id can never
        exceed the current max, so this safely includes every real id
        (for 'all') without excluding any real id from an explicit list,
        regardless of gaps from prior deletions. Cheap fallback of 1000
        only matters for a genuinely empty table."""
        return conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 1000

    def toggle_staging(self, target, selection, state):
        with closing(self.get_conn()) as conn:
            try:
                table = "albums" if target == "album" else "assets"
                total_rows = self._resolve_bound(conn, table)
                ids = list(parse_selection(selection, total_rows))

                placeholders = ",".join("?" for _ in ids)

                if target == "album":
                    cursor = conn.execute(f"UPDATE albums SET is_staged=? WHERE id IN ({placeholders})", [state, *ids])
                    conn.execute(f"UPDATE assets SET is_staged=? WHERE album_id IN ({placeholders})", [state, *ids])
                else:
                    cursor = conn.execute(f"UPDATE assets SET is_staged=? WHERE id IN ({placeholders})", [state, *ids])

                conn.commit()
                verb = "staged" if state else "unstaged"
                # cursor.rowcount, NOT len(ids) — the selection can include ids
                # that don't actually exist (a gap within the MAX(id) bound),
                # which len(ids) would count as "affected" even though the
                # UPDATE matched nothing for them. Confirmed via test: staging
                # a real id + a mid-range gap reported 2 when only 1 changed.
                console.print(f"[green]Successfully {verb} {cursor.rowcount} {target}(s).[/green]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

    def wipe(self, album_ids=None, force=False):
        with closing(self.get_conn()) as conn:
            # Blank AND literal 'all' both mean "wipe everything" — routing
            # 'all' through parse_selection(..., 9999) instead would build a
            # 9999-element id list just to hand it to the confirmation
            # prompt (unreadable) and to a WHERE IN clause (needlessly large).
            if not album_ids or album_ids.strip().lower() == "all":
                if not force and not Confirm.ask("[bold red]WIPE:[/bold red] Delete ALL albums and assets?"):
                    return
                conn.execute("DELETE FROM assets")
                conn.execute("DELETE FROM albums")
                # VACUUM can't run inside a transaction. DELETE opens one
                # implicitly under this connection's default isolation mode —
                # confirmed by testing: without this commit, VACUUM raised
                # "cannot VACUUM from within a transaction", and because that
                # exception fired before any commit, the deletes were rolled
                # back too (row counts unchanged after a "successful"-looking
                # call). This commit is not optional.
                conn.commit()
                conn.execute("VACUUM")
                console.print("[green]Data wiped. Configuration preserved.[/green]")
            else:
                try:
                    total = self._resolve_bound(conn, "albums")
                    ids = list(parse_selection(album_ids, total))
                    if not ids:
                        console.print("[yellow]No valid album ids in selection.[/yellow]")
                        return

                    found = [r[0] for r in conn.execute(
                        f"SELECT id FROM albums WHERE id IN ({','.join('?' for _ in ids)})", ids
                    ).fetchall()]
                    if not found:
                        console.print(f"[yellow]None of the requested album id(s) exist: {ids}[/yellow]")
                        return

                    if not force and not Confirm.ask(f"Delete album ID(s) {found}?"):
                        return

                    placeholders = ",".join("?" for _ in found)
                    conn.execute(f"DELETE FROM assets WHERE album_id IN ({placeholders})", found)
                    cursor = conn.execute(f"DELETE FROM albums WHERE id IN ({placeholders})", found)
                    conn.commit()
                    # cursor.rowcount here, not len(ids) — same reporting-
                    # accuracy issue as toggle_staging otherwise.
                    console.print(f"[green]Deleted {cursor.rowcount} album(s).[/green]")
                except Exception as e:
                    console.print(f"[red]Wipe failed: {e}[/red]")

    def nuke(self):
        if Prompt.ask("[bold red]DANGER:[/bold red] This drops ALL tables. Type [bold]nuke[/bold] to proceed") == "nuke":
            with closing(self.get_conn()) as conn:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
                for t in tables: conn.execute(f"DROP TABLE {t}")
                conn.execute("VACUUM")
            console.print("[green]Database nuked. Schema will auto-rebuild on next use.[/green]")

# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    insp = Inspector()
    parser = argparse.ArgumentParser(description="Bunkr DB Management Toolkit")
    subparsers = parser.add_subparsers(dest="command")

    # VIEW COMMAND
    view = subparsers.add_parser("view", help="View data reports")
    view.add_argument("target", default="dashboard", nargs="?",
                       help="dashboard/albums/expiring/staged/album, or any other table name for a raw dump")
    view.add_argument("--id", type=int, help="Album ID for 'view album'")
    view.add_argument("--limit", "-n", type=int, default=10, help="Row limit for raw table dumps (default 10)")
    view.add_argument("--all", action="store_true", help="Show every row (raw table dumps), ignore --limit")
    view.add_argument("--search", "-s", help="Filter raw table dumps by title (LIKE match)")

    # STAGE COMMAND
    stage = subparsers.add_parser("stage", help="Manage staging status")
    stage.add_argument("target", choices=["album", "asset"])
    stage.add_argument("selection", help="IDs (e.g. 1,2,5-10 or all)")
    stage.add_argument("--off", action="store_true", help="Unstage instead of stage")

    # DB COMMAND
    db_cmd = subparsers.add_parser("db", help="Maintenance operations")
    db_cmd.add_argument("--wipe", nargs="?", const="all", default=None,
                         metavar="IDS", help="Wipe album(s) by ID/comma-list/range, or leave blank for all")
    db_cmd.add_argument("--nuke", action="store_true", help="Factory reset (drops everything)")
    db_cmd.add_argument("--exec", help="Run raw write SQL")
    db_cmd.add_argument("--add-column", metavar="SPEC", help="table:column:type, e.g. assets:true_file_id:INTEGER")
    db_cmd.add_argument("--drop-column", metavar="SPEC", help="table:column, e.g. assets:true_file_id")
    db_cmd.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args()

    if args.command == "view":
        if args.target == "dashboard": insp.display_dashboard()
        elif args.target == "albums": insp.display_albums()
        elif args.target == "expiring": insp.display_expiring()
        elif args.target == "staged": insp.display_staged()
        elif args.target == "album":
            if not args.id: console.print("[red]Error: --id required[/red]"); return
            insp.display_album_detail(args.id)
        else:
            # Not a known keyword — treat it as a real table name.
            insp.display_table(args.target, limit=args.limit, search=args.search, show_all=args.all)

    elif args.command == "stage":
        state = 0 if args.off else 1
        insp.toggle_staging(args.target, args.selection, state)

    elif args.command == "db":
        if args.nuke: insp.nuke()
        elif args.wipe is not None: insp.wipe(args.wipe, args.yes)
        elif args.add_column: insp.add_column(args.add_column)
        elif args.drop_column: insp.drop_column(args.drop_column, args.yes)
        elif args.exec:
            with closing(insp.get_conn()) as conn:
                res = conn.execute(args.exec)
                conn.commit()
                console.print(f"[green]Executed. Rows affected: {res.rowcount}[/green]")
    else:
        insp.display_dashboard()

if __name__ == "__main__":
    main()