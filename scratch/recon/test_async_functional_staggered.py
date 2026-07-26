import argparse
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

import urllib3
from curl_cffi.curl import CurlError
from curl_cffi.requests import AsyncSession
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
console = Console()

def parse_arguments():
    """Parse command line arguments for the staggered functional tester."""
    parser = argparse.ArgumentParser(description="Staggered Functional Asynchronous CDN Token Minter Tester.")
    parser.add_argument(
        '-i', '--input',
        type=str,
        required=True,
        help="Path to the custom indexed album JSON file (e.g., 3_album_name.json)."
    )
    return parser.parse_args()

async def execute_request_retry_async(session, url, method="GET", json_payload=None, headers=None, retries=3, delay=1, timeout=30):
    """Asynchronous request wrapper handling automated CurlError 35 retries."""
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
            console.print(f"  [bold yellow][!][/bold yellow] Network glitch caught ({e}). Retrying in {delay}s... (Attempt {attempt}/{retries})")
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
    
    # --- Step 1: Meta API ---
    meta_url = "https://dl.bunkr.cr/api/_001_v2"
    try:
        meta_res = await execute_request_retry_async(session, meta_url, method="POST", json_payload={"id": str_file_id}, headers=headers)
        meta_data = meta_res.json()
        cdn_host = meta_data["mediafiles"]
        storage_path = meta_data["path"]
        original_name = meta_data["original"]
    except Exception as e:
        progress_bar.console.print(f"    [bold red][-][/bold red] Async Meta failed for ID [dim]{str_file_id}[/dim]: {e}")
        file_item["signed_cdn_url"] = None
        progress_bar.advance(task_id)
        return False

    # --- Step 2: Signing Gateway ---
    encoded_path = urllib.parse.quote(storage_path)
    sign_url = f"https://glb-apisign.cdn.cr/sign?path={encoded_path}"
    try:
        sign_res = await execute_request_retry_async(session, sign_url, method="GET", headers=headers)
        sign_data = sign_res.json()
        token = sign_data["token"]
        ex = sign_data["ex"]
    except Exception as e:
        progress_bar.console.print(f"    [bold red][-][/bold red] Async Sign failed for ID [dim]{str_file_id}[/dim]: {e}")
        file_item["signed_cdn_url"] = None
        progress_bar.advance(task_id)
        return False

    # --- Step 3: URL Stitching ---
    encoded_name = urllib.parse.quote(original_name)
    file_item["signed_cdn_url"] = f"{cdn_host}{storage_path}?n={encoded_name}&token={token}&ex={ex}"
    
    progress_bar.advance(task_id)
    return True

async def test_main():
    args = parse_arguments()
    input_json_path = Path(args.input)

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
    console.print(f"[bold green][+][/bold green] Loaded [bold cyan]{total_files}[/bold cyan] file entries for functional async execution.\n")

    progress_bar = Progress(
        TextColumn("[bold cyan][*][/bold cyan] Staggered Async Minting..."),
        BarColumn(bar_width=40, style="dim white", complete_style="cyan"),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console
    )
    
    sem = asyncio.Semaphore(5) 

    async def worker(session, file_item, pb, tid, idx):
        # Microscopic delay dynamically scaled by item order to cleanly warm up the connection pool
        await asyncio.sleep(idx * 0.05)
        async with sem:
            return await mint_cdn_url_async(session, file_item, pb, tid)

    with progress_bar:
        task_id = progress_bar.add_task("Processing", total=total_files)
        async with AsyncSession(impersonate="chrome") as session:
            # Enumerate the array to map indexes down to the staggered worker queues
            tasks = [worker(session, item, progress_bar, task_id, i) for i, item in enumerate(files_list)]
            results = await asyncio.gather(*tasks)

    minted_count = sum(1 for success in results if success)

    data["files_found"] = files_list
    test_output_path = input_json_path.parent / f"staggered_verify_{input_json_path.name}"
    
    console.print(f"\n[bold yellow][*][/bold yellow] Writing enriched URLs to verification script: [dim white]{test_output_path}[/dim white]...")
    with open(test_output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    console.print(f"[bold green][+][/bold green] Complete! Successfully validated tokens for [bold cyan]{minted_count}/{total_files}[/bold cyan] assets via explicit returns.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.run(test_main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(test_main())