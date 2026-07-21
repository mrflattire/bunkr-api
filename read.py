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

def parse_selection(spec: str, total_files: int) -> set:
    """Parses a choice spec like '1,4-6,9' into a set of 1-based indices."""
    spec_clean = spec.strip().lower()
    if not spec or spec_clean == 'all':
        return set(range(1, total_files + 1))
        
    if spec_clean == 'staged':
        return set()

    selected = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            try:
                start_str, end_str = chunk.split("-", 1)
                start, end = int(start_str), int(end_str)
                if start > end:
                    start, end = end, start
                selected.update(range(start, end + 1))
            except ValueError:
                raise ValueError(f"Invalid range format in chunk: '{chunk}'")
        else:
            try:
                selected.add(int(chunk))
            except ValueError:
                raise ValueError(f"Invalid numeric index: '{chunk}'")

    out_of_range = {i for i in selected if i < 1 or i > total_files}
    if out_of_range:
        raise ValueError(
            f"Index/indices {sorted(out_of_range)} out of range (Valid range is 1-{total_files})."
        )
    return selected

def show_interactive_options(album_id, page_assets, start_idx, total_pages, current_page, total_items):
    """Provides a prompt lifecycle allowing hands-free secondary script operations."""
    has_expired_tokens = False
    for asset in page_assets:
        token_status = parse_and_check_expiry(asset["token_expiry_timestamp"])
        if "Expired" in token_status:
            has_expired_tokens = True
            break

    minter_style = "[bold red blink]Mint new tokens (⚠️ EXPIRED)[/bold red blink]" if has_expired_tokens else "[bold white]Mint new tokens[/bold white]"

    console.print("\n[bold cyan][交互 Engine] Select an Action Context:[/bold cyan]")
    
    nav_hints = []
    if current_page < total_pages: nav_hints.append("[bold white]n[/bold white]: Next Page")
    if current_page > 1: nav_hints.append("[bold white]p[/bold white]: Prev Page")
    if nav_hints: console.print(f" Navigation -> {' | '.join(nav_hints)}")
        
    console.print(" [bold white]1.[/bold white] Stream target(s) [dim](Accepts: 5 | 1,3,5 | 1-5 | staged | Enter for ALL)[/dim]")
    console.print(" [bold white]2.[/bold white] Download target(s) [dim](Accepts: 5 | 3,7,12 | 1-10 | staged)[/dim]")
    console.print(" [bold white]3.[/bold white] Download all assets in this album [green]download.py[/green]")
    console.print(" [bold white]4.[/bold white] Copy link to stdout")
    console.print(f" [bold white]5.[/bold white] {minter_style}")
    console.print(" [bold white]6.[/bold white] Stage/Unstage assets [dim](Accepts: 1-10 or 1,2,5 or all)[/dim]")
    console.print(" [bold white]q.[/bold white] Exit this stage")
    
    choices = ["1", "2", "3", "4", "5", "6", "q"]
    if current_page < total_pages: choices.append("n")
    if current_page > 1: choices.append("p")
    
    action = Prompt.ask("\n[bold cyan][?][/bold cyan] Choose option", choices=choices, default="q")
    
    if action in ("n", "p", "q"): return action
    if action == "1": return action

    # Action 2: Download targeted assets
    if action == "2":
        selection = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter item index, list, range, or 'staged'").strip()
        if not selection: return None

        is_staged_mode = selection.lower() == "staged"
        if not is_staged_mode:
            try: 
                parse_selection(selection, total_items)
            except ValueError as e:
                console.print(f"[bold red][-][/bold red] Operational selection error: {e}")
                time.sleep(1.5)
                return None

        workers = Prompt.ask(f"[bold cyan][?][/bold cyan] Worker concurrency (MAX=5)", default="").strip()
        
        if is_staged_mode:
            cmd = [sys.executable, "download.py", "--db-id", str(album_id), "--staged"]
            console.print(f"\n[bold yellow][*][/bold yellow] Forwarding staged components filter to downloader pipeline: [white]--staged{f' -w {workers}' if workers else ''}[/white]")
        else:
            cmd = [sys.executable, "download.py", "--db-id", str(album_id), "-n", selection]
            console.print(f"\n[bold yellow][*][/bold yellow] Launching targeted execution filter payload: [white]-n {selection}{f' -w {workers}' if workers else ''}[/white]")
            
        if workers: 
            cmd.extend(["-w", workers])
            
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            console.print("\n[bold yellow][!][/bold yellow] Download execution aborted.")
        time.sleep(1)
        return "2"  # signal caller to reload all_assets — download may have minted fresh tokens
    # Action 3: Forward all keys
    elif action == "3":
        workers = Prompt.ask(f"[bold cyan][?][/bold cyan] Worker concurrency (MAX=5)", default="").strip()
        cmd = [sys.executable, "download.py", "--db-id", str(album_id)]
        if workers: cmd.extend(["-w", workers])
        subprocess.run(cmd)
        time.sleep(1)
        return "3"  # signal caller to reload all_assets — download may have minted fresh tokens

    # Action 4: Copy link
    elif action == "4":
        try:
            selection = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter row index ({start_idx}-{start_idx + len(page_assets) - 1})")
            idx = int(selection) - start_idx
            if 0 <= idx < len(page_assets):
                url = page_assets[idx]["signed_cdn_url"] or page_assets[idx]["source_url"] or "N/A"
                console.print(f"\n[bold green][+][/bold green] Endpoint: [bold white]{url}[/bold white]\n")
                Prompt.ask("[dim white]Press Enter...[/dim white]")
        except ValueError: console.print("[bold red][-][/bold red] Invalid selection.")

    # Action 5: Mint tokens
    elif action == "5":
        subprocess.run([sys.executable, "mint.py", "--album-id", str(album_id)])
        return "5"

    # Action 6: Stage / Unstage using inspector.py
    elif action == "6":
        sub_choice = Prompt.ask("[bold cyan][?][/bold cyan] 1: Stage Album | 2: Unstage Album | 3: Stage Assets | 4: Unstage Assets", choices=["1", "2", "3", "4"], default="1")

        if sub_choice == "1": subprocess.run([sys.executable, "inspector.py", "--stage-album", str(album_id)])
        elif sub_choice == "2": subprocess.run([sys.executable, "inspector.py", "--unstage-album", str(album_id)])
        elif sub_choice in ("3", "4"):
            spec = Prompt.ask("[bold cyan][?][/bold cyan] Enter indices/ranges (or 'all')").strip()
            if spec:
                flag = "--stage-assets" if sub_choice == "3" else "--unstage-assets"
                
                if spec.lower() == "all":
                    subprocess.run([sys.executable, "inspector.py", flag, "all"])
                else:
                    try:
                        target_indices = parse_selection(spec, total_items)
                        with db.connection() as conn:
                            all_db_assets = conn.execute("SELECT id FROM assets WHERE album_id = ? ORDER BY track_number ASC;", (album_id,)).fetchall()
                        
                        asset_ids = [str(dict(a)["id"]) for i, a in enumerate(all_db_assets, start=1) if i in target_indices]
                        if asset_ids:
                            subprocess.run([sys.executable, "inspector.py", flag, ",".join(asset_ids)])
                    except ValueError as e: console.print(f"[bold red][-][/bold red] Error: {e}")
        time.sleep(1)
        return "6"

    return None

def render_db_dashboard(album_id):
    """Main rendering dashboard operating against SQLite data states."""
    while True:
        with db.connection() as conn:
            album = conn.execute("SELECT * FROM albums WHERE id = ?;", (album_id,)).fetchone()
            if not album:
                console.print("[bold red][-][/bold red] Database album record missing.")
                return False
            album_dict = dict(album)

        all_assets = db.get_album_assets(album_id)

        staged_badge = " [bold green][STAGED][/bold green]" if album_dict.get("is_staged") == 1 else ""
        summary_text = (
            f"[bold cyan]Source Origin Context:[/bold cyan] {album_dict['search_term'] if album_dict['search_term'] else 'Direct Browsing Link'}\n"
            f"[bold cyan]Album Global Index:[/bold cyan] #{album_dict['global_index']}\n"
            f"[bold cyan]Reported Dataset Size:[/bold cyan] {format_bytes(album_dict['aggregate_size'])} ({album_dict['file_count']} files){staged_badge}"
        )
        console.print(Panel(summary_text, title=f"[bold green]Parsed DB Record: {album_dict['title']}[/bold green]", expand=False))

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
                asset_dict = dict(asset)
                display_name = asset_dict["original_filename"] or asset_dict["title"] or "Unknown Target Asset"
                if asset_dict.get("is_staged") == 1:
                    display_name = f"[bold green][S][/bold green] {display_name}"
                    
                readable_size = format_bytes(asset_dict["raw_size_bytes"])
                target_url = asset_dict["signed_cdn_url"] or asset_dict["source_url"] or "N/A"
                token_status = parse_and_check_expiry(asset_dict["token_expiry_timestamp"])

                table.add_row(str(i), display_name, readable_size, token_status, target_url)

            console.print(table)
            
            nav_action = show_interactive_options(album_id, page_assets, start_idx + 1, total_pages, current_page, total_items)
            
            if nav_action == "q":
                return True
            elif nav_action == "n":
                current_page += 1
            elif nav_action == "p":
                current_page -= 1
            elif nav_action == "1":
                selection = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter item index, list, range, or 'staged' [dim](or Press Enter for ALL)[/dim]").strip()
                if not selection:
                    selection = "all"
                
                is_staged_mode = selection.lower() == "staged"
                if not is_staged_mode and selection.lower() != "all":
                    try:
                        parse_selection(selection, total_items)
                    except ValueError as e:
                        console.print(f"[bold red][-][/bold red] Operational selection error: {e}")
                        time.sleep(1.5)
                        break

                player = Prompt.ask("[bold cyan][?][/bold cyan] Select Media Player Engine", choices=["mpv", "vlc"], default="mpv")
                
                if is_staged_mode:
                    cmd = [sys.executable, "stream.py", "--db-id", str(album_id), "--staged", "--player", player]
                    console.print(f"\n[bold yellow][*][/bold yellow] Forwarding staged selection to streamer pipeline: [white]--staged --player {player}[/white]")
                else:
                    cmd = [sys.executable, "stream.py", "--db-id", str(album_id), "-n", selection, "--player", player]
                    console.print(f"\n[bold yellow][*][/bold yellow] Forwarding selection to streamer pipeline: [white]-n {selection} --player {player}[/white]")
                    
                try:
                    subprocess.run(cmd)
                except KeyboardInterrupt:
                    console.print("\n[bold yellow][!][/bold yellow] Streaming sequence aborted cleanly. Returning to dashboard...")
                time.sleep(1)
                break
            elif nav_action in ("2", "3", "5", "6"):
                break
                
    return True

def launch_scraper_and_get_new_album():
    """
    Runs scrape.py fully interactively (it prompts for search term, mode,
    sort, per-page, and album selection on its own — no flags needed) and
    figures out what it registered by diffing the album list before/after,
    rather than parsing its Rich-formatted console output for the new ID.
    Returns the new album's db id, or None if nothing new was registered
    (user cancelled scrape.py, or it hit an error before syncing).
    """
    before_ids = {a["id"] for a in db.get_all_albums()}

    try:
        subprocess.run([sys.executable, "scrape.py"])
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Scrape session aborted.")

    after_albums = db.get_all_albums()
    new_ones = [a for a in after_albums if a["id"] not in before_ids]

    if not new_ones:
        console.print("[bold yellow][!][/bold yellow] No new album was registered.")
        return None

    if len(new_ones) > 1:
        # register_album_from_json upserts on conflict, so a single scrape.py
        # run registers exactly one album — this is just a defensive fallback
        # in case that assumption ever changes.
        console.print(f"[bold yellow][!][/bold yellow] {len(new_ones)} new albums detected — opening the most recent.")

    return new_ones[0]["id"]


if __name__ == "__main__":
    if os.name == 'nt':
        subprocess.run("", shell=True)

    # 1. CLI Entry Point Setup
    if len(sys.argv) >= 2:
        target_path = clean_dragged_path(sys.argv[1])
        if target_path.endswith('.json') and os.path.exists(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                db_id = db.register_album_from_json(data)
                render_db_dashboard(db_id)
            except Exception as e:
                console.print(f"[bold red][-][/bold red] JSON file registration failed: {e}")
        else:
            try:
                db_id = int(target_path)
                render_db_dashboard(db_id)
            except ValueError:
                console.print("[bold red][-][/bold red] Error: Input must be a valid JSON file path or database record ID.")
    else:
        # 2. Interactive Selector Interface Loop
        while True:
            try:
                albums = db.get_all_albums()
                if not albums:
                    console.print("[bold yellow][!][/bold yellow] No records tracked in database.")
                    console.print()
                    console.print(" [bold white]s.[/bold white] Search for or discover a new album")
                    console.print(" [bold white]q.[/bold white] Exit reader")
                    console.print()
                    target_input = clean_dragged_path(Prompt.ask("[bold cyan][?][/bold cyan] Drop a fresh JSON path, or select an option"))
                    if target_input.lower() in ('q', 'quit', 'exit'):
                        break
                    if target_input.lower() in ('s', 'scrape'):
                        new_id = launch_scraper_and_get_new_album()
                        if new_id:
                            render_db_dashboard(new_id)
                        continue
                    if os.path.exists(target_input):
                        with open(target_input, "r", encoding="utf-8") as f:
                            db_id = db.register_album_from_json(json.load(f))
                        render_db_dashboard(db_id)
                else:
                    console.print("\n[bold magenta][*] Discovered Albums Cataloged in DB:[/bold magenta]")
                    for idx, a in enumerate(albums, start=1):
                        a_dict = dict(a)
                        staged_flag = " [bold green][STAGED][/bold green]" if a_dict.get('is_staged') == 1 else ""
                        console.print(f"  [bold cyan]{idx}[/bold cyan] • {a_dict['title']} ({a_dict['file_count']} items){staged_flag} [dim white](DB ID: {a_dict['id']})[/dim white]")

                    console.print()
                    console.print(" [bold white]s.[/bold white] Search for or discover a new album")
                    console.print(" [bold white]d.[/bold white] Delete an album")
                    console.print(" [bold white]q.[/bold white] Exit reader")
                    console.print()

                    target_input = Prompt.ask("[bold cyan][?][/bold cyan] Choose an album, drop a fresh JSON path, or select an option").strip()
                    if target_input.lower() in ('q', 'quit', 'exit'):
                        break

                    if target_input.lower() in ('s', 'scrape'):
                        new_id = launch_scraper_and_get_new_album()
                        if new_id:
                            render_db_dashboard(new_id)
                        continue

                    if target_input.lower() in ('d', 'delete'):
                        del_input = Prompt.ask("[bold cyan][?][/bold cyan] Album number(s) to delete — single, comma list, or range (or Enter to cancel)").strip()
                        if not del_input:
                            continue

                        # Parse the SAME display-position syntax shown in the
                        # numbered list above (1-based), then map each position
                        # to its real DB id — inspector.py's --wipe-album needs
                        # actual ids, not the display numbers shown on screen.
                        try:
                            display_positions = []
                            for part in del_input.split(","):
                                part = part.strip()
                                if not part:
                                    continue
                                if "-" in part:
                                    start_s, _, end_s = part.partition("-")
                                    start, end = int(start_s.strip()), int(end_s.strip())
                                    if start > end:
                                        start, end = end, start
                                    display_positions.extend(range(start, end + 1))
                                else:
                                    display_positions.append(int(part))
                        except ValueError:
                            console.print("[bold red][-][/bold red] Enter valid album number(s), e.g. 3 or 1,3,5 or 2-4.")
                            continue

                        target_ids = []
                        bad_positions = []
                        for pos in display_positions:
                            idx = pos - 1
                            if 0 <= idx < len(albums):
                                target_ids.append(albums[idx]["id"])
                            else:
                                bad_positions.append(pos)

                        if bad_positions:
                            console.print(f"[bold red][-][/bold red] Invalid album number(s), ignored: {', '.join(str(p) for p in bad_positions)}")

                        if not target_ids:
                            console.print("[bold yellow][!][/bold yellow] No valid album numbers to delete.")
                            continue

                        # inspector.py owns the actual delete + confirmation
                        # prompt (--wipe-album accepts a comma list too now).
                        # Sharing stdio here means its input("Type 'yes'...")
                        # works interactively, same as running it directly.
                        selection_arg = ",".join(str(i) for i in target_ids)
                        subprocess.run([sys.executable, "inspector.py", "--wipe-album", selection_arg])
                        continue

                    target_input = clean_dragged_path(target_input)
                    if not target_input:
                        continue
                        
                    if target_input.endswith('.json') and os.path.exists(target_input):
                        with open(target_input, "r", encoding="utf-8") as f:
                            db_id = db.register_album_from_json(json.load(f))
                        render_db_dashboard(db_id)
                    else:
                        try:
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