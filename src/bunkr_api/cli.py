import argparse
import json
import os
import shlex
import sys
import time
import urllib.parse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

# Internal package imports
from . import inspector as inspect_cmd
from .config import (
    SEARCH_MODES,
    SORT_TYPES,
    TOP_CATEGORIES,
    VALID_COUNTS,
    VERSION,
)
from .core import scraper as scrape_cmd, tokens as mint_cmd
from .core.db import DatabaseManager
from .core.scraper import ScraperEngine
from .core.tokens import refresh_all_tokens_async
from .media import downloader as download_cmd, player as stream_cmd
from .media.downloader import DownloadEngine
from .media.player import PlayerEngine
from .utils.aio import run_async
from .utils.formatting import (
    clean_dragged_path,
    format_bytes,
    parse_and_check_expiry,
    parse_selection,
)

console = Console()
db = DatabaseManager()

def show_interactive_options(album_id, page_assets, start_idx, total_pages, current_page, total_items):
    """Provides a prompt lifecycle allowing hands-free secondary script operations."""
    has_expired_tokens = False
    for asset in page_assets:
        token_status = parse_and_check_expiry(asset["token_expiry_timestamp"])
        if "Expired" in token_status:
            has_expired_tokens = True
            break

    minter_style = " [bold red blink]5. Mint new tokens (⚠️ EXPIRED)[/bold red blink]" if has_expired_tokens else " [bold white]5.[/bold white] Mint new tokens"

    console.print("\n[bold cyan][交互 Engine] Select an Action Context:[/bold cyan]")
    console.print(" Navigation -> [bold white]n[/bold white]: Next Page | [bold white]p[/bold white]: Prev Page")
    console.print(" [bold white]1.[/bold white] Stream target(s) [dim](Accepts: 5 | 3,7,12 | 1-5 | staged | Enter for ALL)[/dim]")
    console.print(" [bold white]2.[/bold white] Download target(s) [dim](Accepts: 5 | 3,7,12 | 1-10 | staged)[/dim]")
    console.print(" [bold white]3.[/bold white] Download ALL assets in this album")
    console.print(" [bold white]4.[/bold white] Copy link to stdout")
    console.print(f"{minter_style} [dim](Manual batch refresh with feedback)[/dim]")
    console.print(" [bold white]6.[/bold white] Stage/Unstage assets [dim](1: Album | 2: Assets)[/dim]")
    console.print(" [bold white]q.[/bold white] Exit this stage")
    
    return Prompt.ask("\n[bold cyan][?][/bold cyan] Choose option", choices=["1", "2", "3", "4", "5", "6", "n", "p", "q"], default="q").lower()

async def show_album_details(album_id):
    """
    Restored: The full detailed Dashboard with all original 7 actions.
    MODIFIED: Now async def to support await calls to engines.
    """
    downloader = DownloadEngine(db)
    player = PlayerEngine(db)
    page_size = 10
    current_page = 1

    while True:
        # 1. Fetch Fresh State
        with db.connection() as conn:
            album = conn.execute("SELECT * FROM albums WHERE id = ?;", (album_id,)).fetchone()
            if not album: 
                console.print("[bold red][!] Database album record missing.[/bold red]")
                break
            album = dict(album)
            assets = db.get_album_assets(album_id)
            total_items = len(assets)
            total_pages = (total_items + page_size - 1) // page_size

        # 2. Render Header
        staged_badge = " [bold green][STAGED][/bold green]" if album.get("is_staged") else ""
        completed_count = sum(1 for x in assets if dict(x).get("download_status") == "COMPLETED")
        completed_badge = (
            " [bold blue][COMPLETED][/bold blue]"
            if total_items > 0 and completed_count == total_items
            else ""
        )
        summary = (
            f"[bold cyan]Origin Context:[/bold cyan] {album['search_term'] or 'Direct Link / Import'}\n"
            f"[bold cyan]Album Global Index:[/bold cyan] #{album['global_index']}\n"
            f"[bold cyan]Reported Dataset Size:[/bold cyan] {album['aggregate_size']} ({album['file_count']} files){staged_badge}{completed_badge}"
        )
        console.print(Panel(summary, title=f"[bold green]Parsed DB Record: {album['title']}[/bold green]", expand=False))

        # 3. Render Table
        start = (current_page - 1) * page_size
        page_assets = assets[start:start+page_size]

        table = Table(title=f"Deep Resolved Assets Inventory (Page {current_page}/{total_pages} | Items {start + 1}-{min(start + page_size, total_items)} of {total_items})", style="dim white")
        table.add_column("#", justify="right", style="magenta")
        table.add_column("Asset Original Name / Storage Name", style="white")
        table.add_column("Size", justify="center", style="green")
        table.add_column("Link Token Lifespan Metric")
        table.add_column("Preferred Content Target URL", style="blue")

        for i, asset in enumerate(page_assets, start=start + 1):
            a = dict(asset)
            status = parse_and_check_expiry(a['token_expiry_timestamp'])
            name = a['title']
            if a.get('is_staged'): 
                name = f"[bold green][S][/bold green] {name}"
            
            table.add_row(
                str(i), name[:50], 
                format_bytes(a['raw_size_bytes']), 
                status, 
                (a['signed_cdn_url'] or a['source_url'] or "N/A")[:35] + "..."
            )
        
        console.print(table)
        
        # 4. Interaction Prompts
        act = show_interactive_options(
            album_id=album_id,
            page_assets=page_assets,
            start_idx=start,
            total_pages=total_pages,
            current_page=current_page,
            total_items=total_items
        )

        if act == 'q': break
        if act == 'n' and current_page < total_pages: current_page += 1; continue
        if act == 'p' and current_page > 1: current_page -= 1; continue

        if act == "1":
            sel = Prompt.ask("[bold cyan][?][/bold cyan] Stream selection (Enter for all)").strip() or "all"
            indices = parse_selection(sel, total_items=total_items)
            p_engine = Prompt.ask("[bold cyan][?][/bold cyan] Media Player Engine", choices=["mpv", "vlc"], default="mpv")
            
            selected_assets = [dict(assets[i-1]) for i in indices]
            # Await the token refresh
            await player.resolve_tokens_async(selected_assets)
            
            queue = []
            for i in indices:
                a = dict(assets[i-1])
                url = db.get_valid_url(a['id'])
                queue.append((i, a['title'], url))
            
            if p_engine == "mpv":
                await player.play_mpv(queue)
            else:
                await player.play_vlc(queue)

        elif act == "2":
            sel = Prompt.ask("[bold cyan][?][/bold cyan] Enter item index, list, or range").strip()
            if not sel: continue
            indices = parse_selection(sel, total_items=total_items)
            workers = IntPrompt.ask("[bold cyan][?][/bold cyan] Worker concurrency (MAX=5)", default=1)
            dl_list = []
            for i in indices:
                d = dict(assets[i-1])
                d['db_asset_id'] = d['id']; d['album_title'] = album['title']; d['album_id'] = album['id']
                dl_list.append(d)
            # Await the downloader
            await downloader.run(dl_list, workers=workers)

        elif act == "3":
            workers = IntPrompt.ask("[bold cyan][?][/bold cyan] Worker concurrency (MAX=5)", default=1)
            dl_list = []
            for a in assets:
                d = dict(a)
                d['db_asset_id'] = d['id']; d['album_title'] = album['title']; d['album_id'] = album['id']
                dl_list.append(d)
            # Await the downloader
            await downloader.run(dl_list, workers=workers)

        elif act == "4":
            sel = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter row index ({start+1}-{start+len(page_assets)})")
            try:
                idx = int(sel); target = dict(assets[idx-1])
                url = target.get("signed_cdn_url") or target.get("source_url")
                console.print(f"\n[bold green][+][/bold green] Endpoint: [bold white]{url}[/bold white]\n")
                Prompt.ask("[dim white]Press Enter to return...[/dim white]")
            except (ValueError, IndexError, KeyboardInterrupt): 
                console.print("[bold red][!] Invalid selection.[/bold red]")

        elif act == "5":
            raw_assets = db.get_needs_refresh(album_id=album_id)
            expiring_assets = [dict(row) for row in raw_assets]
            if expiring_assets:
                max_workers = int(db.get_config_val("max_workers", "4"))
                await refresh_all_tokens_async(db, expiring_assets, max_workers)
                console.print(f"[bold green][+][/bold green] Refreshed {len(expiring_assets)} token(s).")
            else:
                console.print("[dim]No tokens currently need refreshing.[/dim]")
            continue

        elif act == "6":
            sub = Prompt.ask("[bold cyan][?][/bold cyan] 1: Stage Album | 2: Unstage Album | 3: Stage Assets | 4: Unstage Assets", choices=["1","2","3","4"], default="1")
            with db.connection() as conn:
                if sub == "1":
                    conn.execute("UPDATE albums SET is_staged=1 WHERE id=?", (album_id,))
                    conn.execute("UPDATE assets SET is_staged=1 WHERE album_id=?", (album_id,))
                elif sub == "2":
                    conn.execute("UPDATE albums SET is_staged=0 WHERE id=?", (album_id,))
                    conn.execute("UPDATE assets SET is_staged=0 WHERE album_id=?", (album_id,))
                elif sub in ("3","4"):
                    sel = Prompt.ask("[bold cyan][?][/bold cyan] Enter indices/range (or 'all')").strip()
                    idx_list = parse_selection(sel, total_items=total_items)
                    val = 1 if sub == "3" else 0
                    for i in idx_list:
                        conn.execute("UPDATE assets SET is_staged=? WHERE id=?", (val, assets[i-1]['id']))
            console.print("[bold green][+][/bold green] Staging state updated.")
            time.sleep(1)

async def run_scrape_interactive(search_seed=None, mode_seed=None, per_seed=None, sort_seed=None, save_json_seed=None, output_dir_seed=None):
    """Restored: Multi-page search loop with original informative descriptive prompt."""
    scraper = ScraperEngine(db)
    search_term = search_seed if search_seed is not None else Prompt.ask("[bold cyan][?][/bold cyan] Enter search term [dim](Blank for homepage)[/dim]").strip()
    if mode_seed: url_mode = mode_seed
    else:
        mode_choice = Prompt.ask("[bold cyan][?][/bold cyan] Mode", choices=list(SEARCH_MODES.keys()), default="broad").lower()
        url_mode = SEARCH_MODES[mode_choice]
    url_per = per_seed if per_seed is not None else IntPrompt.ask("[bold cyan][?][/bold cyan] Results per page", choices=[str(c) for c in VALID_COUNTS], default=20)
    if sort_seed: url_sort = sort_seed
    else:
        sort_choice = Prompt.ask("[bold cyan][?][/bold cyan] Sort", choices=["latest", "oldest", "most files"], default="latest").lower()
        url_sort = SORT_TYPES[sort_choice]
    save_json = save_json_seed if save_json_seed is not None else (Prompt.ask("[bold cyan][?][/bold cyan] Save JSON backup?", choices=["y", "n"], default="n") == "y")

    async with AsyncSession(impersonate="chrome") as session:
        current_page = 1
        while True:
            query = {'search': search_term, 'mode': url_mode, 'per': str(url_per), 'sort': url_sort}
            if current_page > 1: query['page'] = str(current_page)
            search_url = f"https://balbums.st/?{urllib.parse.urlencode(query)}"
            console.print(f"\n[bold yellow][*][/bold yellow] Loading Search Results (Page {current_page})...")
            
            from .utils.http import execute_request_with_retry_async
            res = await execute_request_with_retry_async(session, search_url)
            soup = BeautifulSoup(res.text, 'html.parser')
            albums = scraper.parse_albums(res.text)
            total_pages = scraper.extract_page_metadata(soup)
            
            if not albums:
                console.print("[bold red][!] No albums discovered.[/bold red]")
                if current_page > 1: current_page -= 1; continue
                return None

            start_idx = ((current_page - 1) * url_per) + 1
            end_idx = start_idx + len(albums) - 1

            display_search = f'"{search_term}"' if search_term else '"Homepage"'
            header_title = f"{display_search} Results Page {current_page} of {total_pages} (Items {start_idx}-{end_idx} loaded) Mode: {url_mode}"
            table = Table(title=header_title, style="dim white")
            table.add_column("#", justify="right", style="magenta")
            table.add_column("Title / Target Album", style="white")
            table.add_column("Files (Est.)", justify="center", style="green")
            table.add_column("Source URL", style="blue")
            
            for i, album in enumerate(albums, start=start_idx):
                table.add_row(str(i), album['title'][:60], album.get('file_count', '???'), album['url'][:40] + "...")
            console.print(table)

            prompt_text = (
                f"\n[bold cyan][?][/bold cyan] Enter selection number ({start_idx}-{end_idx}), "
                f"[bold white]'n'[/bold white] for next page, "
                f"[bold white]'p'[/bold white] for previous page (or [bold red]q[/bold red] to quit)"
            )
            choice = Prompt.ask(prompt_text).strip().lower()

            if choice == 'q': return None
            if choice == 'n': current_page += 1; continue
            if choice == 'p' and current_page > 1: current_page -= 1; continue
            
            try:
                idx = int(choice)
                if start_idx <= idx <= end_idx:
                    return await scraper.scrape_album(
                        session, 
                        albums[idx-start_idx]['url'], 
                        search_term, 
                        album_number_index=idx,
                        save_json=save_json,
                        output_dir=output_dir_seed
                    )
                else:
                    console.print(f"[bold red][!] Selection {idx} is out of range.[/bold red]")
            except (ValueError, IndexError): 
                console.print("[bold red][!] Please enter a valid number or navigation command.[/bold red]")

async def run_top_engine_interactive(category_seed=None, save_json_seed=None, output_dir_seed=None):
    """Restored: Trending loop with original descriptive prompt and flag bypass."""
    scraper = ScraperEngine(db)
    cat = category_seed if category_seed is not None else Prompt.ask("[bold cyan][?][/bold cyan] Category", choices=list(TOP_CATEGORIES.keys()), default="albums")
    lapse = Prompt.ask("[bold cyan][?][/bold cyan] Timeframe", choices=["24h", "7d", "30d", "all"], default="24h")
    save_json = save_json_seed if save_json_seed is not None else (Prompt.ask("[bold cyan][?][/bold cyan] Save JSON backup?", choices=["y", "n"], default="n") == "y")

    async with AsyncSession(impersonate="chrome") as session:
        current_page = 1
        while True:
            top_url = f"https://balbums.st/{TOP_CATEGORIES[cat]}?lapse={lapse}"
            if current_page > 1: top_url += f"&page={current_page}"
            
            console.print(f"\n[bold yellow][*][/bold yellow] Loading Trending {cat.capitalize()}...")
            from .utils.http import execute_request_with_retry_async
            res = await execute_request_with_retry_async(session, top_url)
            items = scraper.parse_top_items(res.text, cat) 
            if not items: return None
            
            start_idx = (current_page - 1) * 15 + 1
            end_idx = start_idx + len(items) - 1
            table = Table(title=f"Trending {cat.capitalize()} ({lapse}) - Page {current_page}", style="dim white")
            table.add_column("#", justify="right", style="magenta"); table.add_column("Title"); table.add_column("Files (Est.)", style="green")
            for i, item in enumerate(items, start_idx):
                table.add_row(str(i), item['title'], item.get('file_count', '1 file'))
            console.print(table)

            prompt_text = (
                f"\n[bold cyan][?][/bold cyan] Enter selection number ({start_idx}-{end_idx}), "
                f"[bold white]'n'[/bold white] for next page, "
                f"[bold white]'p'[/bold white] for previous page (or [bold red]q[/bold red] to quit)"
            )
            choice = Prompt.ask(prompt_text).strip().lower()

            if choice == 'q': return None
            if choice == 'n': current_page += 1; continue
            if choice == 'p' and current_page > 1: current_page -= 1; continue
            
            try:
                idx = int(choice)
                if start_idx <= idx <= end_idx:
                    return await scraper.scrape_album(
                        session, 
                        items[idx-start_idx]['url'], 
                        f"top_{cat}", 
                        album_number_index=idx,
                        save_json=save_json,
                        output_dir=output_dir_seed
                    )
                else:
                    console.print(f"[bold red][!] Selection {idx} is out of range.[/bold red]")
            except (ValueError, IndexError): 
                console.print("[bold red][!] Please enter a valid number or navigation command.[/bold red]")

async def main_loop():
    """Restored: Original Informative vertical menu with Trending support."""
    while True:
        albums = db.get_all_albums()
        console.print("\n[bold magenta][*] Discovered Albums Cataloged in DB:[/bold magenta]")
        if not albums:
            console.print("[dim]  (No records cataloged yet)[/dim]")
        else:
            for i, a in enumerate(albums, start=1):
                a_dict = dict(a)
                staged = " [bold green][STAGED][/bold green]" if a_dict.get('is_staged') else ""
                total_assets = a_dict.get('total_assets') or 0
                completed_assets = a_dict.get('completed_assets') or 0
                completed = " [bold blue][COMPLETED][/bold blue]" if total_assets > 0 and completed_assets == total_assets else ""
                console.print(f"  [bold cyan]{i:2d}[/bold cyan] • [yellow]{a_dict['title']}[/yellow] ({a_dict['file_count']} items){staged}{completed} [dim white](DB ID: {a_dict['id']})[/dim white]")

        console.print()
        console.print(" [bold white]s.[/bold white] Search for or discover a new album")
        console.print(" [bold white]t.[/bold white] Browse trending and top media")
        console.print(" [bold white]d.[/bold white] Delete an album")
        console.print(" [bold white]q.[/bold white] Exit reader")
        console.print()

        raw = Prompt.ask("[bold cyan][?][/bold cyan] Choose an album #, drop a JSON path, or select an option").strip()
        if not raw: continue
        
        processed_path = clean_dragged_path(raw)
        if processed_path.lower().endswith('.json') and os.path.exists(processed_path):
            try:
                with open(processed_path, encoding='utf-8') as f:
                    data = json.load(f)
                new_id, _new_count, _updated_count = db.register_album_from_json(data)
                await show_album_details(new_id)
                continue
            except Exception as e:
                console.print(f"[red][!] JSON import failed: {e}[/red]")
                continue

        cmd = raw.lower()
        if cmd == 'q': break
        if cmd == 's':
            new_id = await run_scrape_interactive()
            if new_id: await show_album_details(new_id)
            continue
        if cmd == 't':
            new_id = await run_top_engine_interactive()
            if new_id: await show_album_details(new_id)
            continue
        
        if cmd == 'd':
            del_spec = Prompt.ask("[bold red][?][/bold red] Album number(s) to delete (e.g. 1, 2-4)")
            if not del_spec: continue
            try:
                indices = parse_selection(del_spec, total_items=len(albums))
                targets = [albums[idx-1] for idx in indices]
                target_ids = [a['id'] for a in targets]
                labels = [f"#{a['id']} \"{a['title']}\"" for a in targets]

                if Prompt.ask(f"Permanently delete {', '.join(labels)}?", choices=["y", "n"], default="n") == "y":
                    with db.connection() as conn:
                        p_holders = ",".join("?" for _ in target_ids)
                        conn.execute(f"DELETE FROM assets WHERE album_id IN ({p_holders})", target_ids)
                        conn.execute(f"DELETE FROM albums WHERE id IN ({p_holders})", target_ids)
                    console.print("[bold green][+][/bold green] Deleted:")
                    for label in labels:
                        console.print(f"  [dim]-[/dim] {label}")
            except Exception as e:
                console.print(f"[red][!] Deletion failed: {e}[/red]")
            continue

        try:
            choice_idx = int(raw)
            if 1 <= choice_idx <= len(albums):
                await show_album_details(albums[choice_idx-1]['id'])
        except (ValueError, IndexError):
            console.print("[red][!] Invalid selection or unknown command.[/red]")

# ============================================================
# Command-line interface
#
#   bunkr-api                 interactive dashboard
#   bunkr-api <command> ...   explicit, scriptable subcommands
#
# Each tool module owns its own options (add_arguments) and behaviour (run);
# this file only wires them together under one parser.
# ============================================================

COMMANDS = ("open", "import", "scrape", "download", "stream", "mint", "inspect")

EXAMPLES = """\
examples:
  bunkr-api                              open the interactive dashboard
  bunkr-api scrape "creator name"        search, catalog an album, open it
  bunkr-api scrape --top albums          browse trending albums
  bunkr-api open 17                      view album 17 from the database
  bunkr-api download --db-id 17 -w 3     download album 17, 3 files at a time
  bunkr-api download --staged            download everything you have staged
  bunkr-api stream --db-id 17 -n 2-4     stream files 2 to 4 of album 17
  bunkr-api inspect view albums          per-album progress report

Run 'bunkr-api <command> -h' for the options of a command.
"""


def cmd_dashboard(args) -> int:
    """No subcommand: the interactive main menu."""
    try:
        run_async(main_loop())
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!] Session terminated gracefully.[/bold yellow]")
    return 0


def cmd_open(args) -> int:
    run_async(show_album_details(args.album_id))
    return 0


def cmd_import(args) -> int:
    path = clean_dragged_path(args.file)
    if not os.path.exists(path):
        console.print(f"[bold red][!] File not found: {path}[/bold red]")
        return 1
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        new_id, _new_count, _updated_count = db.register_album_from_json(data)
    except Exception as e:
        console.print(f"[bold red][!] Import failed: {e}[/bold red]")
        return 1
    run_async(show_album_details(new_id))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bunkr-api",
        description=(
            f"Bunkr API Manager {VERSION}\n\n"
            "Run with no command to open the interactive dashboard, or pick a command below."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    parser.set_defaults(func=cmd_dashboard)

    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")

    def add(name, help_text, module=None, run=None, **kw):
        p = sub.add_parser(
            name, help=help_text, description=help_text[0].upper() + help_text[1:] + ".",
            formatter_class=argparse.RawDescriptionHelpFormatter, **kw,
        )
        if module is not None:
            module.add_arguments(p)
        p.set_defaults(func=run or module.run)
        return p

    add("scrape", "search Bunkr (or browse trending) and catalog an album", scrape_cmd)
    add("download", "download an album, your staged files, or failed files", download_cmd)
    add("stream", "stream an album or your staged files in mpv/vlc", stream_cmd)
    add("open", "view an album that is already in the database", run=cmd_open) \
        .add_argument("album_id", type=int, metavar="ID", help="database ID of the album")
    add("import", "import a legacy JSON metadata file and view it", run=cmd_import) \
        .add_argument("file", metavar="FILE", help="path to the JSON file")
    add("mint", "refresh expiring download tokens (daemon, or one album)", mint_cmd)
    add("inspect", "database reports, staging and maintenance", inspect_cmd)
    return parser


# ------------------------------------------------------------
# Legacy invocations (pre-subcommand CLI)
#
# `bunkr-api 17`, `bunkr-api "query"`, `bunkr-api -i file.json`, `--db-id 17`
# and top-level scrape flags still work: they are translated to the matching
# subcommand with a notice on stderr. Delete this block (and its call in
# main) to drop the compatibility layer.
# ------------------------------------------------------------

class _QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _legacy_parser() -> argparse.ArgumentParser:
    p = _QuietParser(prog="bunkr-api", add_help=False)
    p.add_argument("search", nargs="?")
    p.add_argument("-m", "--mode")
    p.add_argument("-p", "--per")
    p.add_argument("-s", "--sort")
    p.add_argument("-t", "--top", nargs="?", const="prompt")
    p.add_argument("--save-json", action="store_true")
    p.add_argument("-o", "--output")
    p.add_argument("--db-id", type=int)
    p.add_argument("-i", "--input")
    return p


def _translate_legacy(argv: list[str]) -> tuple[list[str], str | None]:
    """Map an old-style argv onto the subcommand CLI.

    Returns (argv, hint); *hint* is the equivalent new command when a
    translation happened, else None.
    """
    passthrough = ("-h", "--help", "-v", "--version")
    if not argv or argv[0] in COMMANDS or any(a in passthrough for a in argv):
        return argv, None

    try:
        ns = _legacy_parser().parse_args(argv)
    except ValueError:
        ns = None  # let the real parser report the problem against 'scrape'

    if ns is not None:
        json_path = ns.input or (ns.search if ns.search and ns.search.endswith(".json") else None)
        album_id = ns.db_id if ns.db_id is not None else (
            ns.search if ns.search and ns.search.isdigit() else None
        )
        if json_path:
            new = ["import", json_path]
        elif album_id is not None:
            new = ["open", str(album_id)]
        else:
            new = ["scrape", *argv]
    else:
        new = ["scrape", *argv]

    return new, "bunkr-api " + shlex.join(new)


def main(argv: list[str] | None = None) -> int:
    """Entry point for `bunkr-api`. Returns the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)

    argv, hint = _translate_legacy(argv)
    if hint:
        print(f"note: this form is deprecated; use: {hint}", file=sys.stderr)

    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
