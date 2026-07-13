import sys
import json
import os
import time
from datetime import datetime
import urllib.parse
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()

def format_bytes(num_bytes):
    """Converts raw integer bytes into a clean, human-readable string format."""
    if not isinstance(num_bytes, (int, float)):
        return str(num_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"

def parse_and_check_expiry(url_str):
    """Parses a URL to extract and evaluate the security token's lifespan."""
    if not url_str or url_str == "N/A":
        return "[dim white]No token found[/dim white]"
        
    try:
        parsed = urllib.parse.urlparse(url_str)
        params = urllib.parse.parse_qsl(parsed.query)
        query_dict = dict(params)
        
        ex_val = query_dict.get('ex')
        if not ex_val:
            return "[yellow]Signed (No Expiry Found)[/yellow]"
            
        expiry_timestamp = int(ex_val)
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
    except Exception:
        return "[bold yellow]Signed (Check Link)[/bold yellow]"

def show_interactive_options(filepath, all_files, page_files, start_idx, total_pages, current_page):
    """
    Provides a prompt lifecycle allowing hands-free secondary script operations.
    Returns: 'n' (next), 'p' (prev), 'q' (quit), '3' (remint), or None to loop again.
    """
    console.print("\n[bold cyan][交互 Engine] Select an Action Context:[/bold cyan]")
    
    # Show page navigation hints dynamically
    nav_hints = []
    if current_page < total_pages:
        nav_hints.append("[bold white]n[/bold white]: Next Page")
    if current_page > 1:
        nav_hints.append("[bold white]p[/bold white]: Prev Page")
    if nav_hints:
        console.print(f" Navigation -> {' | '.join(nav_hints)}")
        
    console.print(" [bold white]1.[/bold white] Forward all asset keys to [green]downloader.py[/green]")
    console.print(" [bold white]2.[/bold white] Copy a specific item link directly to console standard output")
    console.print(" [bold white]3.[/bold white] Remint expired tokens via [green]advanced_async_cdn_sign_minter.py[/green]")
    console.print(" [bold white]4.[/bold white] Stream a specific asset target directly via [green]mpv[/green]")
    console.print(" [bold white]q.[/bold white] Close reader utility context frame")
    
    choices = ["1", "2", "3", "4", "q"]
    if current_page < total_pages: choices.append("n")
    if current_page > 1: choices.append("p")
    
    action = Prompt.ask("\n[bold cyan][?][/bold cyan] Choose option", choices=choices, default="q")
    
    if action in ("n", "p", "q", "3"):
        return action
        
    if action == "1":
        console.print(f"\n[bold yellow][*][/bold yellow] Handing file over to execution context: [dim white]python downloader.py {filepath}[/dim white]")
        os.system(f"python downloader.py --input {filepath}")
    elif action in ("2", "4"):
        try:
            # Allow user to pick the literal global `#` row number shown in the table
            end_idx = start_idx + len(page_files) - 1
            selection = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter item row index ({start_idx}-{end_idx})")
            idx = int(selection) - 1
            if start_idx - 1 <= idx <= end_idx:
                chosen_file = all_files[idx]
                target_url = chosen_file.get("signed_cdn_url") or chosen_file.get("href", "N/A")
                
                if action == "2":
                    console.print(f"\n[bold green][+][/bold green] Extracted Asset Stream Endpoint:\n\n[bold white]{target_url}[/bold white]\n")
                    Prompt.ask("[dim white]Press Enter to continue...[/dim white]")
                elif action == "4":
                    if "Expired" in parse_and_check_expiry(chosen_file.get("signed_cdn_url")):
                        console.print("[bold red][-][/bold red] Error: Cannot stream an expired token asset. Remint tokens first.")
                        time.sleep(2)
                    else:
                        console.print(f"\n[bold yellow][*][/bold yellow] Initializing mpv asset pipeline for: [white]{chosen_file.get('original') or chosen_file.get('title')}[/white]")
                        # Pass the link directly to mpv using system execution context
                        os.system(f'mpv "{target_url}"')
            else:
                console.print("[bold red][-][/bold red] Selected row boundary error limits crossed.")
                time.sleep(1.5)
        except ValueError:
            console.print("[bold red][-][/bold red] Invalid selection entry pattern initialized.")
            time.sleep(1.5)
    return None

def read_and_render_json(filepath):
    if not os.path.exists(filepath):
        console.print(f"[bold red][-][/bold red] Error: Target payload mapping path '{filepath}' not discovered.")
        return

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Parsing failure inside file: {e}")
        return

    search_term = data.get("search_term", "N/A")
    album = data.get("selected_album", {})
    all_files = data.get("files_found", [])

    # Step 1: Render Global Header Metadata Block Panel ONCE at startup
    summary_text = (
        f"[bold cyan]Source Origin Context:[/bold cyan] {search_term if search_term else '[dim white]Direct Browsing Link[/dim white]'}\n"
        f"[bold cyan]Album Global Index:[/bold cyan] #{album.get('album_index_number', 'N/A')}\n"
        f"[bold cyan]Reported Dataset Size:[/bold cyan] {album.get('aggregate_size', 'Unknown')} ({album.get('clean_file_count', '0 files')})"
    )
    console.print(Panel(summary_text, title=f"[bold green]Parsed Record: {album.get('title', 'Unknown Title')}[/bold green]", expand=False))

    if not all_files:
        console.print("[bold yellow][!][/bold yellow] No item records discovered inside target sequence tracking arrays.")
        return

    # Pagination configuration
    page_size = 10
    total_items = len(all_files)
    total_pages = (total_items + page_size - 1) // page_size
    current_page = 1

    # --- Inline Stream Pagination Loop ---
    while True:
        # Mathematical slice for current page items
        start_idx = (current_page - 1) * page_size
        end_idx = start_idx + page_size
        page_files = all_files[start_idx:end_idx]

        # Step 2: Render Assets Table Matrix for current slice
        table = Table(
            title=f"\n[bold magenta]Deep Resolved Assets Inventory (Page {current_page}/{total_pages} | Items {start_idx + 1}-{min(end_idx, total_items)} of {total_items})[/bold magenta]", 
            style="dim white"
        )
        table.add_column("#", justify="right", style="magenta")
        table.add_column("Asset Original Name / Storage Name", style="white")
        table.add_column("Size", justify="center", style="green")
        table.add_column("Link Token Lifespan Metric", justify="left")
        table.add_column("Preferred Content Streaming Target URL", style="blue")

        for i, file_rec in enumerate(page_files, start=start_idx + 1):
            display_name = file_rec.get("original") or file_rec.get("title") or "Unknown Target Asset"
            raw_size = file_rec.get("size", "N/A")
            readable_size = format_bytes(raw_size) if isinstance(raw_size, int) else str(raw_size)
            
            target_url = file_rec.get("signed_cdn_url") or file_rec.get("href", "N/A")
            token_status = parse_and_check_expiry(file_rec.get("signed_cdn_url"))

            table.add_row(
                str(i),
                display_name,
                readable_size,
                token_status,
                target_url
            )

        console.print(table)
        
        # Step 3: Trigger Interactive Options
        nav_action = show_interactive_options(filepath, all_files, page_files, start_idx + 1, total_pages, current_page)
        
        if nav_action == "q":
            break
        elif nav_action == "n":
            current_page += 1
        elif nav_action == "p":
            current_page -= 1
        elif nav_action == "3":
            console.print(f"\n[bold yellow][*][/bold yellow] Launching token minter: [dim white]python advanced_async_cdn_sign_minter.py --input {filepath}[/dim white]")
            os.system(f"python advanced_async_cdn_sign_minter.py --input {filepath}")
            console.print("[bold green][+][/bold green] Minter execution finished. Reloading dataset...")
            
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    reloaded_data = json.load(f)
                all_files = reloaded_data.get("files_found", [])
                total_items = len(all_files)
                total_pages = (total_items + page_size - 1) // page_size
                if current_page > total_pages: 
                    current_page = max(1, total_pages)
            except Exception as e:
                console.print(f"[bold red][-][/bold red] Failed to reload JSON payload: {e}")
                
            time.sleep(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        console.print("[bold yellow][!][/bold yellow] Usage: [green]python reader.py <path_to_json_file>[/green]")
    else:
        read_and_render_json(sys.argv[1])