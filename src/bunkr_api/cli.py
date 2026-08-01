import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup
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

# Internal package imports
from .core.db import DatabaseManager
from .core.scraper import ScraperEngine
from .core.tokens import refresh_all_tokens_async
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
        summary = (
            f"[bold cyan]Origin Context:[/bold cyan] {album['search_term'] or 'Direct Link / Import'}\n"
            f"[bold cyan]Album Global Index:[/bold cyan] #{album['global_index']}\n"
            f"[bold cyan]Reported Dataset Size:[/bold cyan] {album['aggregate_size']} ({album['file_count']} files){staged_badge}"
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
                console.print(f"  [bold cyan]{i:2d}[/bold cyan] • [yellow]{a_dict['title']}[/yellow] ({a_dict['file_count']} items){staged} [dim white](DB ID: {a_dict['id']})[/dim white]")

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

async def _run():
    """
    MASTER CLI ENTRY POINT (async implementation)
    (Matches logic from original read.py main block)
    """
    parser = argparse.ArgumentParser(
        prog="bunkr-api",
        description=f"Bunkr API Manager {VERSION} - Interactive Dashboard & Master CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Master Help - Related Binaries:
  bunk-api         Dive straight into the main interactive CLI.
  bunkr-scrape     Search bunkr by creator name.
  bunkr-stream     Stream content from a database ID (bunkr-stream --db-id 3).
  bunkr-download   Batch download content from a database ID (bunkr-download --db-id 3).
  bunkr-inspect    Database maintenance and reporting.
  bunkr-mint       Open concurrent token minting daemon.
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
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_JSON_DIR, help="Target directory to save JSON")

    # Working Flags
    parser.add_argument('--db-id', type=int, help="Jump directly to an album in database by ID.")
    parser.add_argument('-i', '--input', type=str, help="Import a legacy JSON file and view it.")
    
    # NOTE: previously there was a second `path` positional here meant to
    # catch a bare numeric ID or JSON path. Since `search` (declared above)
    # is also an optional positional, argparse always assigns a single bare
    # token to `search` first — `path` could never actually receive it
    # (confirmed empirically). Removed in favor of interpreting args.search
    # itself below, in priority order: JSON path -> numeric ID -> search term.

    args = parser.parse_args()

    # Route 1: Handle Import (-i or a bare positional ending in .json)
    target_path = args.input or (args.search if args.search and args.search.endswith('.json') else None)
    if target_path:
        processed = clean_dragged_path(target_path)
        if os.path.exists(processed):
            try:
                with open(processed, encoding='utf-8') as f:
                    data = json.load(f)
                new_id, _new_count, _updated_count = db.register_album_from_json(data)
                await show_album_details(new_id)
                return
            except Exception as e:
                console.print(f"[bold red][!] Import failed: {e}[/bold red]")
                sys.exit(1)

    # Route 2: Handle Direct Jump (--db-id or a bare numeric positional)
    raw_val = args.db_id or (int(args.search) if args.search and args.search.isdigit() else None)
    if raw_val:
        await show_album_details(raw_val)
        return

    # Route 3: Handle Scraper Command Direct Executions
    url_mode = SEARCH_MODES.get(args.mode) if args.mode else None
    if args.search or args.top or args.mode or args.save_json or args.output != DEFAULT_JSON_DIR:
        if args.top:
            album_id = await run_top_engine_interactive(
                category_seed=args.top if args.top != "prompt" else None,
                save_json_seed=args.save_json,
                output_dir_seed=args.output
            )
        else:
            album_id = await run_scrape_interactive(
                search_seed=args.search,
                mode_seed=url_mode,
                per_seed=args.per,
                sort_seed=args.sort,
                save_json_seed=args.save_json,
                output_dir_seed=args.output
            )
        if album_id:
            await show_album_details(album_id)
        return

    # Default: Interactive Main Menu Loop
    try:
        await main_loop()
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!] Session terminated gracefully.[/bold yellow]")
        sys.exit(0)

def main():
    """
    Synchronous entry point. This is what pyproject.toml's [project.scripts]
    'bunkr-api = "bunkr_api.cli:main"' actually calls — entry-point scripts
    invoke the target function directly, they do NOT await coroutines. So
    this must stay a plain sync function that wraps the real async logic in
    asyncio.run(), same pattern as downloader.py/player.py/tokens.py's main().
    """
    loop_f = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
    if loop_f:
        asyncio.run(_run(), loop_factory=loop_f)
    else:
        asyncio.run(_run())

if __name__ == "__main__":
    main()