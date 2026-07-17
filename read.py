# read.py
import sys
import json
import os
import time
import subprocess
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt

# Import core manager and the isolated utility library functions
from core import DatabaseManager
from utils import format_bytes, clean_dragged_path, extract_expiry_from_url

console = Console()
db = DatabaseManager()

def parse_and_check_expiry(expiry_timestamp):
    """Evaluates the security token's lifespan using database-native Unix timestamps."""
    if not expiry_timestamp:
        return "[dim white]No token found[/dim white]"
        
    current_timestamp = int(time.time())
    if current_timestamp > expiry_timestamp:
        return "[bold red]Expired ❌[/bold red]"
        
    # Calculate human-readable remaining time
    remaining = expiry_timestamp - current_timestamp
    hours = remaining // 3600
    mins = (remaining % 3600) // 60
    
    if hours > 0:
        return f"[bold green]Valid ({hours}h {mins}m left) ✅[/bold green]"
    return f"[bold yellow]Valid ({mins}m left) ⚠️[/bold yellow]"

def show_interactive_options(album_id, page_assets, start_idx, total_pages, current_page):
    """
    Provides a prompt lifecycle allowing hands-free secondary script operations.
    Runs seamlessly off of database entity ids.
    """
    has_expired_tokens = False
    for asset in page_assets:
        token_status = parse_and_check_expiry(asset["token_expiry_timestamp"])
        if "Expired" in token_status:
            has_expired_tokens = True
            break

    # Highlight Option 5 if auto_minter loop hasn't swept yet
    if has_expired_tokens:
        minter_style = "[bold red blink]mint.py (⚠️ EXPIRED TOKENS DETECTED)[/bold red blink]"
    else:
        minter_style = "[green]mint.py[/green]"

    console.print("\n[bold cyan][交互 Engine] Select an Action Context:[/bold cyan]")
    
    nav_hints = []
    if current_page < total_pages:
        nav_hints.append("[bold white]n[/bold white]: Next Page")
    if current_page > 1:
        nav_hints.append("[bold white]p[/bold white]: Prev Page")
    if nav_hints:
        console.print(f" Navigation -> {' | '.join(nav_hints)}")
        
    console.print(" [bold white]1.[/bold white] Stream target asset(s) via [green]stream.py[/green] [dim] (Accepts: 5 | 1,3,5 | 1-5 | Enter for ALL)[/dim]")
    console.print(" [bold white]2.[/bold white] Download target asset(s) via [green]download.py[/green] [dim] (Accepts: 5 | 3,7,12 | 1-10)[/dim]")
    console.print(" [bold white]3.[/bold white] Forward all asset keys to [green]download.py[/green]")
    console.print(" [bold white]4.[/bold white] Copy a specific item link directly to console standard output")
    console.print(f" [bold white]5.[/bold white] Mint new tokens via {minter_style}")
    console.print(" [bold white]q.[/bold white] Exit reader")
    
    choices = ["1", "2", "3", "4", "5", "q"]
    if current_page < total_pages: choices.append("n")
    if current_page > 1: choices.append("p")
    
    action = Prompt.ask("\n[bold cyan][?][/bold cyan] Choose option", choices=choices, default="q")
    
    if action in ("n", "p", "q"):
        return action
        
    if action == "5":
        return "5"

    # Action 1: Stream targets via streamer pipeline
    if action == "1":
        selection = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter item index, list, or range [dim](or Press Enter for ALL)[/dim]").strip()
        if not selection:
            selection = "all"

        player = Prompt.ask("[bold cyan][?][/bold cyan] Select Media Player Engine", choices=["mpv", "vlc"], default="mpv")

        console.print(f"\n[bold yellow][*][/bold yellow] Forwarding selection to streamer pipeline: [white]-n {selection} --player {player}[/white]")
        try:
            subprocess.run([sys.executable, "stream.py", "--db-id", str(album_id), "-n", selection, "--player", player])
        except KeyboardInterrupt:
            console.print("\n[bold yellow][!][/bold yellow] Streaming sequence aborted cleanly. Returning to dashboard...")

        time.sleep(1)
        return "1"  # signal caller to reload all_assets — same contract as "5"

    # Action 2: Download targeted assets
    elif action == "2":
        selection = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter item index, list, or range [dim](e.g. 5 or 3,7,12 or 1-10)[/dim]").strip()
        if not selection:
            console.print("[bold red][-][/bold red] Empty target configuration passed.")
            time.sleep(1)
            return None

        workers = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter worker concurrency (MAX=5) [dim](Press Enter for default)[/dim]").strip()
        
        cmd = [sys.executable, "download.py", "--db-id", str(album_id), "-n", selection]
        if workers:
            cmd.extend(["-w", workers])

        console.print(f"\n[bold yellow][*][/bold yellow] Launching targeted execution filter payload: [white]-n {selection}{f' -w {workers}' if workers else ''}[/white]")
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            console.print("\n[bold yellow][!][/bold yellow] Targeted selection run aborted. Returning to dashboard...")
        time.sleep(1)

    # Action 3: Forward all asset keys to downloader
    elif action == "3":
        workers = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter worker concurrency (MAX=5) [dim](Press Enter for default)[/dim]").strip()
        
        cmd = [sys.executable, "download.py", "--db-id", str(album_id)]
        if workers:
            cmd.extend(["-w", workers])

        console.print(f"\n[bold yellow][*][/bold yellow] Handing file over to execution context: [dim white]python download.py --db-id {album_id}{f' -w {workers}' if workers else ''}[/dim white]")
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            console.print("\n[bold yellow][!][/bold yellow] downloader pipeline interrupted cleanly. Returning to dashboard...")
            time.sleep(1)

    # Action 4: Copy specific link directly to stdout console
    elif action == "4":
        try:
            end_idx = start_idx + len(page_assets) - 1
            selection = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter item row index ({start_idx}-{end_idx})")
            idx = int(selection) - start_idx
            if 0 <= idx < len(page_assets):
                chosen_asset = page_assets[idx]
                target_url = chosen_asset["signed_cdn_url"] or chosen_asset["source_url"] or "N/A"
                console.print(f"\n[bold green][+][/bold green] Extracted Asset Stream Endpoint:\n\n[bold white]{target_url}[/bold white]\n")
                Prompt.ask("[dim white]Press Enter to continue...[/dim white]")
            else:
                console.print("[bold red][-][/bold red] Selected row boundary error limits crossed.")
                time.sleep(1.5)
        except ValueError:
            console.print("[bold red][-][/bold red] Invalid selection entry pattern.")
            time.sleep(1.5)

    return None

def render_db_dashboard(album_id):
    """Main rendering dashboard operating against SQLite data states."""
    while True:
        with db._get_connection() as conn:
            album = conn.execute("SELECT * FROM albums WHERE id = ?;", (album_id,)).fetchone()
            if not album:
                console.print("[bold red][-][/bold red] Database album record missing.")
                return False

        all_assets = db.get_album_assets(album_id)

        summary_text = (
            f"[bold cyan]Source Origin Context:[/bold cyan] {album['search_term'] if album['search_term'] else 'Direct Browsing Link'}\n"
            f"[bold cyan]Album Global Index:[/bold cyan] #{album['global_index']}\n"
            f"[bold cyan]Reported Dataset Size:[/bold cyan] {format_bytes(album['aggregate_size'])} ({album['file_count']} files)"
        )
        console.print(Panel(summary_text, title=f"[bold green]Parsed DB Record: {album['title']}[/bold green]", expand=False))

        if not all_assets:
            console.print("[bold yellow][!][/bold yellow] No item records discovered inside database assets table.")
            return True

        page_size = 10
        total_items = len(all_assets)
        total_pages = (total_items + page_size - 1) // page_size
        current_page = 1

        while True:
            start_idx = (current_page - 1) * page_size
            end_idx = start_idx + page_size
            page_assets = all_assets[start_idx:end_idx]

            console.print(f"\n[bold cyan]Deep Resolved Assets Inventory (Page {current_page}/{total_pages} | Items {start_idx + 1}-{min(end_idx, total_items)} of {total_items})[/bold cyan]")
            
            table = Table(style="dim white", show_header=True)
            table.add_column("#", justify="right", style="magenta")
            table.add_column("Asset Original Name / Storage Name", style="white")
            table.add_column("Size", justify="center", style="green")
            table.add_column("Link Token Lifespan Metric", justify="left")
            table.add_column("Preferred Content Streaming Target URL", style="blue")

            for i, asset in enumerate(page_assets, start=start_idx + 1):
                display_name = asset["original_filename"] or asset["title"] or "Unknown Target Asset"
                readable_size = format_bytes(asset["raw_size_bytes"])
                
                target_url = asset["signed_cdn_url"] or asset["source_url"] or "N/A"
                token_status = parse_and_check_expiry(asset["token_expiry_timestamp"])

                table.add_row(
                    str(i),
                    display_name,
                    readable_size,
                    token_status,
                    target_url
                )

            console.print(table)
            
            nav_action = show_interactive_options(album_id, page_assets, start_idx + 1, total_pages, current_page)
            
            if nav_action == "q":
                return True
            elif nav_action == "n":
                current_page += 1
            elif nav_action == "p":
                current_page -= 1
            elif nav_action == "1":
                # stream.py may have minted fresh tokens via the escape hatch —
                # break to the outer loop so all_assets reloads from disk.
                break
            elif nav_action == "5":
                console.print(f"\n[bold yellow][*][/bold yellow] Launching token minter for DB ID: {album_id}")
                try:
                    # UPDATED: Swapped --db-id to --album-id to match mint.py's argument structure
                    subprocess.run([sys.executable, "mint.py", "--album-id", str(album_id)])
                except KeyboardInterrupt:
                    console.print("\n[bold red][-][/bold red] Minter execution interrupted.")
                    
                console.print("[bold green][+][/bold green] Minter execution finished. Reloading database state...")
                break # Break out of pagination loop to query fresh database data states
                
    return True

if __name__ == "__main__":
    if os.name == 'nt':
        subprocess.run("", shell=True)

    # 1. CLI Entry Point
    if len(sys.argv) >= 2:
        target_path = clean_dragged_path(sys.argv[1])
        if target_path.endswith('.json') and os.path.exists(target_path):
            # Parse and register the payload directly into SQLite
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                db_id = db.register_album_from_json(data)
                render_db_dashboard(db_id)
            except Exception as e:
                console.print(f"[bold red][-][/bold red] JSON file registration failed: {e}")
        else:
            # Check if user input is an existing DB ID directly
            try:
                db_id = int(target_path)
                render_db_dashboard(db_id)
            except ValueError:
                console.print("[bold red][-][/bold red] Error: Input must be a valid JSON file path or database record ID.")
    else:
        # 2. Interactive DB Album Record Selector
        while True:
            try:
                albums = db.get_all_albums()
                if not albums:
                    console.print("[bold yellow][!][/bold yellow] No records tracked in database. Drag and drop a payload JSON file here to register it.")
                    target_input = clean_dragged_path(Prompt.ask("[bold cyan][?][/bold cyan] Enter path to JSON file (or 'q' to exit)"))
                    if target_input.lower() in ('q', 'quit', 'exit'):
                        break
                    if os.path.exists(target_input):
                        with open(target_input, "r", encoding="utf-8") as f:
                            db_id = db.register_album_from_json(json.load(f))
                        render_db_dashboard(db_id)
                else:
                    console.print("\n[bold magenta][*] Discovered Albums Cataloged in DB:[/bold magenta]")
                    for idx, a in enumerate(albums, start=1):
                        console.print(f"  [bold cyan]{idx}[/bold cyan] • {a['title']} ({a['file_count']} items) [dim white](DB ID: {a['id']})[/dim white]")
                    console.print()

                    target_input = Prompt.ask("[bold cyan][?][/bold cyan] Choose a record number, drop a fresh JSON path, or 'q' to exit").strip()
                    if target_input.lower() in ('q', 'quit', 'exit'):
                        break
                        
                    target_input = clean_dragged_path(target_input)
                    if not target_input:
                        continue
                        
                    if target_input.endswith('.json') and os.path.exists(target_input):
                        with open(target_input, "r", encoding="utf-8") as f:
                            db_id = db.register_album_from_json(json.load(f))
                        render_db_dashboard(db_id)
                    else:
                        try:
                            # Map selected row back to true album ID
                            choice_idx = int(target_input) - 1
                            if 0 <= choice_idx < len(albums):
                                render_db_dashboard(albums[choice_idx]["id"])
                            else:
                                console.print("[bold red][-][/bold red] Invalid record index selection.")
                        except ValueError:
                            console.print("[bold red][-][/bold red] Unknown command option passed.")
            except KeyboardInterrupt:
                console.print("\n[bold yellow][!][/bold yellow] Reader session terminated cleanly.")
                break