import argparse
import sqlite3
import sys
import time
from contextlib import closing
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

# import from other modules
from .core.db import DatabaseManager
from .utils.formatting import format_bytes, parse_selection

console = Console()

class Inspector:
    def __init__(self):
        self.db = DatabaseManager()

    def get_conn(self):
        return self.db._get_connection()

    
    ##READ-ONLY REPORTS
    

    def display_table(self, table_name, limit=10, search=None, show_all=False):
       
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
            
            summary = Table(title="[magenta]Database Volume & Pipeline Summary[/magenta]", expand=True, style="dim")
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
                
                elif table == "system_config":
                    try:
                        kvs = conn.execute("SELECT config_key, config_value FROM system_config;").fetchall()
                        metrics = ", ".join([f"[bold white]{r['config_key']}[/bold white]={r['config_value']}" for r in kvs])
                    except sqlite3.OperationalError:
                        pass
                
                summary.add_row(table, str(count), metrics)
            
            console.print(Panel(summary, border_style="dim magenta", title="[bold white]Media Tracker Management System[/bold white]"))

    def display_albums(self):
        """Per-album breakdown table."""
        with closing(self.get_conn()) as conn:
            rows = conn.execute("SELECT * FROM albums ORDER BY id ASC;").fetchall()
            if not rows:
                console.print("[yellow]No albums cataloged.[/yellow]")
                return

            t = Table(title="[magenta]Per-Album Breakdown[/magenta]", expand=True, style="dim")
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
        
        t = Table(title="[dim]Expiring/Expired Tokens[/dim]", style="dim")
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
                t = Table(title="[dim magenta]Staged Albums (album-level flag)[/dim magenta]", expand=True, style="dim")
                t.add_column("ID", justify="right", style="cyan")
                t.add_column("Title")
                t.add_column("Files", justify="right")
                for a in staged_albums:
                    t.add_row(str(a["id"]), a["title"], str(a["file_count"]))
                console.print(t)
            else:
                console.print("[dim]No albums flagged staged at the album level.[/dim]")

            if staged_by_album:
                t = Table(title="[dim magenta]Staged Assets by Album[/dim magenta]", expand=True, style="dim")
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

    
    ## SCHEMA MIGRATIONS
    

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

    
    ## WRITE OPERATIONS
    

    def _resolve_bound(self, conn, table):
        """Shared MAX(id) bound for parse_selection; a real safety 
        for gaps from prior deletions. Cheap fallback of 1000
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
                
                console.print(f"[green]Successfully {verb} {cursor.rowcount} {target}(s).[/green]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

    def wipe(self, album_ids=None, force=False):
        with closing(self.get_conn()) as conn:
            
            if not album_ids or album_ids.strip().lower() == "all":
                if not force and not Confirm.ask("[bold red]WIPE:[/bold red] Delete ALL albums and assets?"):
                    return
                conn.execute("DELETE FROM assets")
                conn.execute("DELETE FROM albums")
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


## CLI ENTRY POINT


def main():
    insp = Inspector()
    parser = argparse.ArgumentParser(description="Bunkr DB Management Toolkit")
    subparsers = parser.add_subparsers(dest="command", metavar="{view,stage,db}")

    # VIEW COMMAND
    view = subparsers.add_parser(
        "view", help="View album & file details (bunkr-inspect view -h for more help)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bunkr-inspect view                      quick summary of database\n"
            "  bunkr-inspect view dashboard            quick summary of database\n"
            "  bunkr-inspect view albums               per-album summary of completion %, staged count, size\n"
            "  bunkr-inspect view expiring             albums with tokens expired or expiring soon\n"
            "  bunkr-inspect view staged               quick view of what is staged, grouped by album\n"
            "  bunkr-inspect view album --id 5         full detail for album 5 + all its files\n"
            "  bunkr-inspect view assets               raw dump of the 'assets' (files) table (any table name also works)\n"
            "  bunkr-inspect view assets --all         every file row, not just the first 10\n"
            "  bunkr-inspect view assets -n 25         first 25 files rows instead of the default 10\n"
            "  bunkr-inspect view assets --search foo  only files whose title contains 'foo'\n"
        ),
    )
    view.add_argument(
        "target", default="dashboard", nargs="?", metavar="TARGET",
        help=(
            "What to show. One of the named reports — dashboard, albums, "
            "expiring, staged, album (requires --id) — or any other table "
            "name (e.g. assets) for a raw row-by-row dump of that table. "
            "Defaults to 'dashboard' if omitted."
        )
    )
    view.add_argument("--id", type=int, metavar="ALBUM_ID",
                       help="Album id to show full detail for — only used with 'view album'")
    view.add_argument("--limit", "-n", type=int, default=10, metavar="N",
                       help="Row limit for raw table dumps, e.g. 'view assets -n 25' (default 10, ignored with --all)")
    view.add_argument("--all", action="store_true",
                       help="Raw table dumps only: show every row instead of the --limit cutoff")
    view.add_argument("--search", "-s", metavar="TEXT",
                       help="Raw table dumps only: filter to rows whose title contains TEXT (case-sensitive LIKE match)")

    # STAGE COMMAND
    stage = subparsers.add_parser(
        "stage", help="Stage (or unstage) albums or files(assets) for download/streaming (bunkr-inspect stage -h for more help)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bunkr-inspect stage album 5             stage album 5 (cascades to all its assets)\n"
            "  bunkr-inspect stage album 5 --off       unstage album 5 (cascades too)\n"
            "  bunkr-inspect stage asset 14            stage a single file (asset) by id\n"
            "  bunkr-inspect stage asset 14,15,22      stage a comma-separated list of asset ids\n"
            "  bunkr-inspect stage asset 10-20         stage a range of asset ids, inclusive\n"
            "  bunkr-inspect stage asset all           stage every asset in the database\n"
        ),
    )
    stage.add_argument(
        "target", choices=["album", "asset"], metavar="{album,asset}",
        help="Whether SELECTION refers to album ids or asset ids. Staging an album cascades to all its assets."
    )
    stage.add_argument(
        "selection", metavar="SELECTION",
        help=(
            "Which id(s) to stage/unstage: a single id (14), a comma list "
            "(14,15,22), a range (10-20), a mix of both (1-5,8,12-14), or "
            "the literal word 'all'."
        )
    )
    stage.add_argument("--off", action="store_true", help="Unstage instead of stage (default action is to stage)")

    # DB COMMAND
    db_cmd = subparsers.add_parser(
        "db", help="DB maintenance operations: wipe, nuke, raw SQL, schema changes (bunkr-inspect db -h for more help)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bunkr-inspect --wipe                  delete ALL albums+assets, keep config, prompts first\n"
            "  bunkr-inspect --wipe 12,13            delete only albums 12 and 13 (and their assets)\n"
            "  bunkr-inspect --wipe 7-9 -y           same, but skip the confirmation prompt\n"
            "  bunkr-inspect --nuke                  factory reset: drops every table, including config\n"
            "  bunkr-inspect --exec \"DELETE FROM assets WHERE download_status='FAILED';\"\n"
            "  bunkr-inspect --add-column assets:true_file_id:INTEGER\n"
            "  bunkr-inspect --drop-column assets:true_file_id -y\n"
        ),
    )
    db_cmd.add_argument("--wipe", nargs="?", const="all", default=None,
                         metavar="IDS", help="Wipe album(s) by id/comma-list/range, or leave blank ('--wipe' alone) for every album")
    db_cmd.add_argument("--nuke", action="store_true", help="Factory reset — drops every table including system_config")
    db_cmd.add_argument("--exec", metavar="SQL", help="Run an arbitrary write SQL statement, e.g. an UPDATE or DELETE")
    db_cmd.add_argument("--add-column", metavar="TABLE:COLUMN:TYPE", help="e.g. assets:true_file_id:INTEGER")
    db_cmd.add_argument("--drop-column", metavar="TABLE:COLUMN", help="e.g. assets:true_file_id")
    db_cmd.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts (for --wipe/--nuke/--drop-column)")

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
            # this is not a known keyword. it as a real table name.
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