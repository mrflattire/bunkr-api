import json
import asyncio
import argparse
import urllib.parse
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from curl_cffi.requests import AsyncSession
from curl_cffi.curl import CurlError
import urllib3

from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.prompt import Prompt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
console = Console()

def parse_arguments():
    """Parse command line arguments for the signature minter script."""
    parser = argparse.ArgumentParser(description="Asynchronous CDN Token Signature Minter Utility.")
    parser.add_argument(
        '-i', '--input',
        type=str,
        required=False,  # Made optional to allow the interactive prompt fallback
        help="Path to the custom indexed album JSON file (e.g., 3_album_name.json)."
    )
    return parser.parse_args()

def clean_dragged_path(raw: str) -> str:
    """
    Normalizes a path typed or drag-and-dropped into the terminal.
    Handles surrounding whitespace, quotes, and backslash-escaped spaces.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    text = text.replace("\\ ", " ")
    return text

def prompt_for_input():
    """
    Scans the working directory for payload JSONs, prints them out cleanly,
    and then prompts the user for the input path.
    """
    # 1. Scan and print local .json files
    try:
        json_files = [f for f in os.listdir('.') if f.endswith('.json') and os.path.isfile(f)]
        if json_files:
            console.print("\n[bold magenta][*] Discovered payload JSON targets in working directory:[/bold magenta]")
            for f in sorted(json_files):
                console.print(f"  • [yellow]{f}[/yellow]")
            console.print()
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Warning: Could not scan directory for JSON files: {e}")

    # 2. Prompt for path to the album JSON
    while True:
        raw = Prompt.ask("[bold cyan][?][/bold cyan] Path to the album JSON file")
        candidate = Path(clean_dragged_path(raw)).expanduser()
        if candidate.exists() and candidate.is_file():
            return candidate
        console.print(f"[bold red][-][/bold red] Error: '{candidate}' doesn't exist or isn't a file. Try again.")

async def execute_request_with_retry_async(session, url, method="GET", json_payload=None, headers=None, retries=3, delay=1, timeout=30):
    """Wrapper to handle asynchronous request executions with unified CurlError 35 fallback loops"""
    for attempt in range(1, retries + 1):
        try:
            if method.upper() == "POST":
                res = await session.post(url, json=json_payload, headers=headers, verify=False, timeout=timeout)
            else:
                res = await session.get(url, headers=headers, timeout=timeout)
            res.raise_for_status()
            return res
        except CurlError as e:
            if attempt == retries:
                raise e
            await asyncio.sleep(delay)

async def mint_cdn_url_async(session, file_item, progress_bar, task_id):
    """Asynchronously processes a single file item and returns True if successful, False otherwise."""
    file_id = file_item.get("true_file_id")
    title = file_item.get("title", "Unknown")
    
    if file_id is None or file_id == "":
        progress_bar.console.print(f"  [bold yellow][!][/bold yellow] Skipping [dim white]'{title[:40]}'[/dim white] - Missing true_file_id.")
        file_item["signed_cdn_url"] = None
        progress_bar.advance(task_id)
        return False

    str_file_id = str(file_id)
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://dl.bunkr.cr",
        "Referer": f"https://dl.bunkr.cr/file/{str_file_id}",
    }
    
    meta_url = "https://dl.bunkr.cr/api/_001_v2"
    payload = {"id": str_file_id}
    
    # --- Step 1: Query Metadata API ---
    try:
        meta_res = await execute_request_with_retry_async(session, meta_url, method="POST", json_payload=payload, headers=headers)
        meta_data = meta_res.json()
        
        cdn_host = meta_data["mediafiles"]
        storage_path = meta_data["path"]
        original_name = meta_data["original"]
    except Exception as e:
        progress_bar.console.print(f"    [bold red][-][/bold red] Metadata lookup failed for ID [dim]{str_file_id}[/dim]: {e}")
        file_item["signed_cdn_url"] = None
        progress_bar.advance(task_id)
        return False

    # --- Step 2: Request Dynamic Validation Token ---
    encoded_path = urllib.parse.quote(storage_path)
    sign_url = f"https://glb-apisign.cdn.cr/sign?path={encoded_path}"
    
    try:
        sign_res = await execute_request_with_retry_async(session, sign_url, method="GET", headers=headers)
        sign_data = sign_res.json()
        
        token = sign_data["token"]
        ex = sign_data["ex"]
    except Exception as e:
        progress_bar.console.print(f"    [bold red][-][/bold red] Token signature generation failed for ID [dim]{str_file_id}[/dim]: {e}")
        file_item["signed_cdn_url"] = None
        progress_bar.advance(task_id)
        return False

    # --- Step 3: URL Stitching ---
    encoded_name = urllib.parse.quote(original_name)
    file_item["signed_cdn_url"] = f"{cdn_host}{storage_path}?n={encoded_name}&token={token}&ex={ex}"
    
    progress_bar.advance(task_id)
    return True

async def batch_process_signatures_async():
    # Detect if we need to fall back to the interactive prompt
    if len(sys.argv) == 1:
        input_json_path = prompt_for_input()
    else:
        args = parse_arguments()
        # Fallback to interactive prompt if the input flag was somehow passed as empty/None
        if not args.input:
            input_json_path = prompt_for_input()
        else:
            input_json_path = Path(clean_dragged_path(args.input)).expanduser()

    if not input_json_path.exists():
        console.print(f"[bold red][-][/bold red] Error: Could not find '[bold white]{input_json_path}[/bold white]'.")
        return

    console.print(f"[bold yellow][*][/bold yellow] Loading database file targets from [dim white]{input_json_path}[/dim white]...")
    with open(input_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    files_list = data.get("files_found", [])
    if not files_list:
        console.print("[bold red][-][/bold red] No items found in the 'files_found' key inside the JSON structure.")
        return

    total_files = len(files_list)
    console.print(f"[bold green][+][/bold green] Loaded [bold cyan]{total_files}[/bold cyan] file entries for concurrent token minting.\n")

    progress_bar = Progress(
        TextColumn("[bold yellow][*][/bold yellow] Minting tokens..."),
        BarColumn(bar_width=40, style="dim white", complete_style="green"),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console
    )

    sem = asyncio.Semaphore(5)

    async def worker(session, file_item, pb, tid, idx):
        # Pool Warmup: Stagger only the initiation of the first 5 workers
        # to let sockets negotiate initial TLS context comfortably.
        if idx < 5:
            await asyncio.sleep(idx * 0.15)
        async with sem:
            return await mint_cdn_url_async(session, file_item, pb, tid)

    # Context managers separated to adhere strictly to sync/async operational limits
    with progress_bar:
        task_id = progress_bar.add_task("Processing", total=total_files)
        async with AsyncSession(impersonate="chrome") as session:
            tasks = [worker(session, item, progress_bar, task_id, i) for i, item in enumerate(files_list)]
            results = await asyncio.gather(*tasks)

    # Evaluate execution results without using unregulated global mutations
    minted_count = sum(1 for success in results if success)
    data["files_found"] = files_list

    console.print(f"\n[bold yellow][*][/bold yellow] Writing enriched URLs directly back to [dim white]{input_json_path}[/dim white]...")
    with open(input_json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    console.print(f"[bold green][+][/bold green] Complete! Successfully minted tokens for [bold cyan]{minted_count}/{total_files}[/bold cyan] assets.")

    if minted_count > 0:
        expiry_time = datetime.now() + timedelta(hours=2)
        formatted_expiry = expiry_time.strftime("%I:%M %p (%Y-%m-%d)")
        console.print("  Temporary links will completely expire in exactly [bold yellow]2 hours[/bold yellow].")
        console.print(f"  You have up to [bold yellow]{formatted_expiry}[/bold yellow] to use these links.")

if __name__ == "__main__":
    # Modern execution entry point for Python 3.12+ / 3.14+ to prevent deprecation logs
    if sys.platform == 'win32':
        asyncio.run(batch_process_signatures_async(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(batch_process_signatures_async())