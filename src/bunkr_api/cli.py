import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

from curl_cffi.requests import AsyncSession
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from .config import (
    DEFAULT_JSON_DIR,
    SEARCH_MODES,
    SORT_TYPES,
    TOP_CATEGORIES,
    VALID_COUNTS,
    VERSION,
)

# Import internally
from .core.db import DatabaseManager
from .core.scraper import ScraperEngine
from .core.tokens import daemon_loop
from .media.downloader import DownloadEngine
from .media.player import PlayerEngine
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

def show_album_details(album_id):
    """
    Restored: The full detailed Dashboard with all original 7 actions.
    (Matches the logic of render_db_dashboard from read.py)
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
        summary = (
            f"[bold cyan]Origin Context:[/bold cyan] {album['search_term'] or 'Direct Link / Import'}\n"
            f"[bold cyan]Album Global Index:[/bold cyan] #{album['global_index']}\n"
            f"[bold cyan]Reported Dataset Size:[/bold cyan] {album['aggregate_size']} ({album['file_count']} files){staged_badge}"
        )
        console.print(Panel(summary, title=f"[bold green]Parsed DB Record: {album['title']}[/bold green]", expand=False))

        # 3. Render Table
        start = (current_page - 1) * page_size
        page_assets = assets[start:start+page_size]

        table = Table(title=f"Deep Resolved Assets Inventory (Page {current_page}/{total_pages})", style="dim white")
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
        
        # 4. Action Menu via show_interactive_options
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

        # Action 1: Stream
        if act == "1":
            sel = Prompt.ask("[bold cyan][?][/bold cyan] Stream selection (Enter for all)").strip() or "all"
            indices = parse_selection(sel, total_items=total_items)
            p_engine = Prompt.ask("[bold cyan][?][/bold cyan] Media Player Engine", choices=["mpv", "vlc"], default="mpv")
            
            selected_assets = [dict(assets[i-1]) for i in indices]
            loop_f = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
            asyncio.run(player.resolve_tokens_async(selected_assets), loop_factory=loop_f) if loop_f else asyncio.run(player.resolve_tokens_async(selected_assets))
            
            queue = []
            for i in indices:
                a = dict(assets[i-1])
                url = db.get_valid_url(a['id'])
                queue.append((i, a['title'], url))
            
            player.play_mpv(queue) if p_engine == "mpv" else player.play_vlc(queue)

        # Action 2: Download Targeted
        elif act == "2":
            sel = Prompt.ask("[bold cyan][?][/bold cyan] Enter item index, list, or range").strip()
            if not sel: continue
            indices = parse_selection(sel, total_items)
            workers = IntPrompt.ask("[bold cyan][?][/bold cyan] Worker concurrency (MAX=5)", default=1)
            dl_list = []
            for i in indices:
                d = dict(assets[i-1])
                d['db_asset_id'] = d['id']
                d['album_title'] = album['title']
                d['album_id'] = album['id']
                dl_list.append(d)
            downloader.run(dl_list, workers=workers)

        # Action 3: Download All
        elif act == "3":
            workers = IntPrompt.ask("[bold cyan][?][/bold cyan] Worker concurrency (MAX=5)", default=1)
            dl_list = []
            for a in assets:
                d = dict(a)
                d['db_asset_id'] = d['id']
                d['album_title'] = album['title']
                d['album_id'] = album['id']
                dl_list.append(d)
            downloader.run(dl_list, workers=workers)

        # Action 4: Copy Link
        elif act == "4":
            sel = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter row index ({start+1}-{start+len(page_assets)})")
            try:
                idx = int(sel)
                target = dict(assets[idx-1])
                url = target.get("signed_cdn_url") or target.get("source_url")
                console.print(f"\n[bold green][+][/bold green] Endpoint: [bold white]{url}[/bold white]\n")
                Prompt.ask("[dim white]Press Enter to return...[/dim white]")
            except (ValueError, KeyboardInterrupt): 
                console.print("[bold red][!] Invalid selection.[/bold red]")

        # Action 5: Mint (Feedback restored via daemon_loop)
        elif act == "5":
            daemon_loop(album_id=album_id)
            # Control returns here after progress bar completes. continue reloads DB data.
            continue

        # Action 6: Stage/Unstage
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
                    idx_list = parse_selection(sel, total_items)
                    val = 1 if sub == "3" else 0
                    for i in idx_list:
                        conn.execute("UPDATE assets SET is_staged=? WHERE id=?", (val, assets[i-1]['id']))
            console.print("[bold green][+][/bold green] Staging state updated in database.")
            time.sleep(1)

async def run_scrape_interactive(search_seed=None, mode_seed=None, per_seed=None, sort_seed=None, save_json_seed=None, output_dir_seed=None):
    """
    Multi-page search loop. 
    Bypasses individual prompts if seeds (flags) are provided.
    """
    scraper = ScraperEngine(db)
    
    # 1. Handle Search Term
    search_term = search_seed if search_seed is not None else Prompt.ask("[bold cyan][?][/bold cyan] Enter search term [dim](Blank for homepage)[/dim]").strip()
    
    # 2. Handle Mode
    if mode_seed:
        url_mode = mode_seed
    else:
        mode_choice = Prompt.ask("[bold cyan][?][/bold cyan] Mode", choices=list(SEARCH_MODES.keys()), default="broad").lower()
        url_mode = SEARCH_MODES[mode_choice]
        
    # 3. Handle Per-Page
    url_per = per_seed if per_seed is not None else IntPrompt.ask("[bold cyan][?][/bold cyan] Results per page", choices=[str(c) for c in VALID_COUNTS], default=20)
    
    # 4. Handle Sort
    if sort_seed:
        url_sort = sort_seed
    else:
        sort_choice = Prompt.ask("[bold cyan][?][/bold cyan] Sort", choices=["latest", "oldest", "most files"], default="latest").lower()
        url_sort = SORT_TYPES[sort_choice]
    
    # 5. Handle Save JSON
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
            albums = scraper.parse_albums(res.text)
            
            if not albums:
                console.print("[bold red][!] No albums discovered.[/bold red]")
                if current_page > 1: current_page -= 1; continue
                return None

            table = Table(title=f"Search Results (Page {current_page})", style="dim white")
            table.add_column("#", justify="right", style="magenta")
            table.add_column("Title / Target Album", style="white")
            table.add_column("Files (Est.)", justify="center", style="green")
            table.add_column("Source URL", style="blue")
            
            start_idx = ((current_page - 1) * url_per) + 1
            end_idx = start_idx + len(albums) - 1
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
            except ValueError: 
                console.print("[bold red][!] Please enter a valid number or navigation command.[/bold red]")

async def run_top_engine_interactive(category_seed=None, save_json_seed=None, output_dir_seed=None):
    """Restored: Trending loop with original descriptive prompt and flag bypass."""
    scraper = ScraperEngine(db)
    
    # 1. Handle category to skip prompt if category_seed exists
    cat = category_seed if category_seed is not None else Prompt.ask(
        "[bold cyan][?][/bold cyan] Category", 
        choices=list(TOP_CATEGORIES.keys()), 
        default="albums"
    )
    
    # 2. Handle timeframe
    lapse = Prompt.ask("[bold cyan][?][/bold cyan] Timeframe", choices=["24h", "7d", "30d", "all"], default="24h")
    
    # 3. Handle Save JSON to skip prompt if save_json_seed exists
    save_json = save_json_seed if save_json_seed is not None else (
        Prompt.ask("[bold cyan][?][/bold cyan] Save JSON backup?", choices=["y", "n"], default="n") == "y"
    )

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
            table.add_column("#", justify="right", style="magenta")
            table.add_column("Title")
            table.add_column("Files (Est.)", style="green")
            
            for i, item in enumerate(items, start=start_idx):
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
            except ValueError: 
                console.print("[bold red][!] Please enter a valid number or navigation command.[/bold red]")

def main_loop():
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
                console.print(f"  [bold cyan]{i:2d}[/bold cyan] • [yellow]{a_dict['title']}[/yellow] ({a_dict['file_count']} items){staged} [dim white](DB ID: {a_dict['id']})[/dim white]")

        console.print()
        console.print(" [bold white]s.[/bold white] Search for or discover a new album")
        console.print(" [bold white]t.[/bold white] Browse trending and top media")
        console.print(" [bold white]d.[/bold white] Delete an album")
        console.print(" [bold white]q.[/bold white] Exit reader")
        console.print()

        raw = Prompt.ask("[bold cyan][?][/bold cyan] Choose an album #, drop a JSON path, or select an option").strip()
        if not raw: continue
        
        # drag-and-drop or manual JSON path dealer
        processed_path = clean_dragged_path(raw)
        if processed_path.lower().endswith('.json') and os.path.exists(processed_path):
            try:
                with open(processed_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                new_id = db.register_album_from_json(data)
                show_album_details(new_id)
                continue
            except Exception as e: # noqa: BLE001
                console.print(f"[red][!] JSON import failed: {e}[/red]")
                continue

        cmd = raw.lower()
        if cmd == 'q': break
        if cmd == 's':
            new_id = asyncio.run(run_scrape_interactive())
            if new_id: show_album_details(new_id)
            continue
        if cmd == 't':
            new_id = asyncio.run(run_top_engine_interactive())
            if new_id: show_album_details(new_id)
            continue
        
        if cmd == 'd':
            del_spec = Prompt.ask("[bold red][?][/bold red] Album number(s) to delete (e.g. 1, 2-4)")
            if not del_spec: continue
            try:
                indices = parse_selection(del_spec, total_items=len(albums))
                target_ids = [albums[idx-1]['id'] for idx in indices]
                
                if Prompt.ask(f"Permanently delete {len(target_ids)} album(s)?", choices=["y", "n"], default="n") == "y":
                    with db.connection() as conn:
                        p_holders = ",".join("?" for _ in target_ids)
                        conn.execute(f"DELETE FROM assets WHERE album_id IN ({p_holders})", target_ids)
                        conn.execute(f"DELETE FROM albums WHERE id IN ({p_holders})", target_ids)
                    console.print("[bold green][+][/bold green] Deletion successful.")
            except Exception as e:  # noqa: BLE001 
                console.print(f"[red][!] Deletion failed: {e}[/red]")
            continue

        try:
            choice_idx = int(raw)
            if 1 <= choice_idx <= len(albums):
                show_album_details(albums[choice_idx-1]['id'])
        except ValueError:
            console.print("[red][!] Invalid selection or unknown command.[/red]")

def main():
    """
    MASTER CLI ENTRY POINT
    (Matches logic from original read.py main block)
    """
    parser = argparse.ArgumentParser(
        prog="bunkr-api",
        description=f"Bunkr API Manager {VERSION} - Interactive Dashboard & Master CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Master Help - Related Binaries:
  bunk-api         Dive straight into the main interactive CLI; search, scrape, stream, download
  bunkr-scrape     Search bunkr by creator name and add any of their albums to your DB .
  bunkr-stream     Go straight to stream menu or direct stream with ID (bunkr-stream --db-id 3).
  bunkr-download   Go straight to download menu or direct download with ID (bunkr-download --db-id 3).
  bunkr-inspect    Database maintenance and reporting.
  bunkr-mint       Open concurrent token minting daemon in another terminal window.
        """
    )
    # Core Metadata
    parser.add_argument('-v', '--version', action='version', version=f'%(prog)s {VERSION}')

    # Search & Scraping Flags
    parser.add_argument("search", nargs="?", default=None, help="The search query")
    parser.add_argument("-m", "--mode", choices=list(SEARCH_MODES.keys()), help="Search mode")
    parser.add_argument("-p", "--per", type=int, choices=VALID_COUNTS, help="Results per page")
    parser.add_argument("-s", "--sort", choices=["latest", "oldest", "mostfiles"], help="Sorting metric")
    parser.add_argument("-t", "--top", nargs="?", const="prompt", help="Trending category")
    parser.add_argument("--save-json", action="store_true", help="Save backup JSON")
    parser.add_argument(
        "-o", "--output", 
        type=Path, 
        default=DEFAULT_JSON_DIR, 
        help="Target directory to save JSON metadata"
    )

    # Working Flags
    parser.add_argument('--db-id', type=int, help="Jump directly to an album in database by ID. Leave out --db-id for the same effect")
    parser.add_argument('-i', '--input', type=str, help="Import a legacy JSON file and view it.")
    
    # Catch bare positional argument from original read.py
    parser.add_argument('path', nargs='?', help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Route 1: Handle Import (-i or positional JSON path)
    target_path = args.input or (args.path if args.path and args.path.endswith('.json') else None)
    if target_path:
        processed = clean_dragged_path(target_path)
        if os.path.exists(processed):
            try:
                with open(processed, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                new_id = db.register_album_from_json(data)
                show_album_details(new_id)
                return
            except Exception as e: # noqa: BLE001 
                console.print(f"[bold red][!] Import failed: {e}[/bold red]")
                sys.exit(1)

    # Route 2: Handle Direct Jump (--db-id or positional numeric ID)
    raw_val = args.db_id or (int(args.path) if args.path and args.path.isdigit() else None)
    if raw_val:
        show_album_details(raw_val)
        return

    # Route 3: Handle Scraper Command Direct Executions (if search or top flags were passed)
    url_mode = SEARCH_MODES.get(args.mode) if args.mode else None
    if args.search or args.top or args.mode or args.save_json or args.output != DEFAULT_JSON_DIR:
        async def _run():
            if args.top:
                return await run_top_engine_interactive(
                    category_seed=args.top if args.top != "prompt" else None,
                    save_json_seed=args.save_json,
                    output_dir=args.output
                )
            else:
                return await run_scrape_interactive(
                    search_seed=args.search,
                    mode_seed=url_mode,
                    per_seed=args.per,
                    sort_seed=args.sort,
                    save_json_seed=args.save_json,
                    output_dir=args.output
                )

        try:
            album_id = asyncio.run(_run())
            if album_id:
                show_album_details(album_id)
            return
        except KeyboardInterrupt:
            sys.exit(0)

    # Default: Interactive Main Menu Loop
    try:
        main_loop()
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!] Session terminated gracefully.[/bold yellow]")
        sys.exit(0)

if __name__ == "__main__":
    main()