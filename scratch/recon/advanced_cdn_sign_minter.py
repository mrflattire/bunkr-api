import json
import time
import argparse
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta
from curl_cffi import requests
from curl_cffi.curl import CurlError  # Catch connection reset/TLS level exceptions
import urllib3

from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
console = Console()

def parse_arguments():
    """Parse command line arguments for the signature minter script."""
    parser = argparse.ArgumentParser(description="CDN Token Signature Minter Utility.")
    parser.add_argument(
        '-i', '--input',
        type=str,
        required=True,
        help="Path to the custom indexed album JSON file (e.g., 3_album_name.json)."
    )
    return parser.parse_args()

def execute_request_with_retry(session, url, method="GET", json_payload=None, headers=None, retries=3, delay=1, timeout=30):
    """Wrapper to handle synchronous request executions with unified CurlError 35 fallback loops"""
    for attempt in range(1, retries + 1):
        try:
            if method.upper() == "POST":
                res = session.post(url, json=json_payload, headers=headers, verify=False, timeout=timeout)
            else:
                res = session.get(url, headers=headers, timeout=timeout)
            res.raise_for_status()
            return res
        except CurlError as e:
            if attempt == retries:
                raise e
            time.sleep(delay)

def mint_cdn_url(session, file_id):
    str_file_id = str(file_id)
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://dl.bunkr.cr",
        "Referer": f"https://dl.bunkr.cr/file/{str_file_id}",
    }
    
    meta_url = "https://dl.bunkr.cr/api/_001_v2"
    payload = {"id": str_file_id}
    
    # --- Step 1: Query Metadata API with Retry wrapper ---
    try:
        meta_res = execute_request_with_retry(session, meta_url, method="POST", json_payload=payload, headers=headers)
        meta_data = meta_res.json()
        
        cdn_host = meta_data["mediafiles"]
        storage_path = meta_data["path"]
        original_name = meta_data["original"]
    except Exception as e:
        console.print(f"    [bold red][-][/bold red] Metadata lookup failed for ID [dim]{str_file_id}[/dim]: {e}")
        return None

    # --- Step 2: Request Dynamic Validation Token with Retry wrapper ---
    encoded_path = urllib.parse.quote(storage_path)
    sign_url = f"https://glb-apisign.cdn.cr/sign?path={encoded_path}"
    
    try:
        sign_res = execute_request_with_retry(session, sign_url, method="GET", headers=headers)
        sign_data = sign_res.json()
        
        token = sign_data["token"]
        ex = sign_data["ex"]
    except Exception as e:
        console.print(f"    [bold red][-][/bold red] Token signature generation failed for ID [dim]{str_file_id}[/dim]: {e}")
        return None

    encoded_name = urllib.parse.quote(original_name)
    return f"{cdn_host}{storage_path}?n={encoded_name}&token={token}&ex={ex}"

def batch_process_signatures():
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
    console.print(f"[bold green][+][/bold green] Loaded [bold cyan]{total_files}[/bold cyan] file entries for token minting.\n")

    minted_count = 0

    progress_bar = Progress(
        TextColumn("[bold yellow][*][/bold yellow] Minting tokens..."),
        BarColumn(bar_width=40, style="dim white", complete_style="green"),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console
    )

    with requests.Session(impersonate="chrome") as session, progress_bar:
        task_id = progress_bar.add_task("Processing", total=total_files)

        for file_item in files_list:
            file_id = file_item.get("true_file_id")
            title = file_item.get("title", "Unknown")
            
            if file_id is None or file_id == "":
                progress_bar.console.print(f"  [bold yellow][!][/bold yellow] Skipping [dim white]'{title[:40]}'[/dim white] - Missing true_file_id.")
                file_item["signed_cdn_url"] = None
                progress_bar.advance(task_id)
                continue
                
            signed_url = mint_cdn_url(session, file_id)
            
            if signed_url:
                file_item["signed_cdn_url"] = signed_url
                minted_count += 1
            else:
                file_item["signed_cdn_url"] = None
            
            progress_bar.advance(task_id)

    data["files_found"] = files_list

    console.print(f"\n[bold yellow][*][/bold yellow] Writing enriched URLs back to [dim white]{input_json_path}[/dim white]...")
    with open(input_json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    console.print(f"[bold green][+][/bold green] Complete! Successfully minted tokens for [bold cyan]{minted_count}/{total_files}[/bold cyan] assets.")

    if minted_count > 0:
        expiry_time = datetime.now() + timedelta(hours=2)
        formatted_expiry = expiry_time.strftime("%I:%M %p (%Y-%m-%d)")
        console.print("  Temporary links will completely expire in exactly [bold yellow]2 hours[/bold yellow].")
        console.print(f"  You have up to [bold yellow]{formatted_expiry}[/bold yellow] to use these links.")
        
if __name__ == "__main__":
    batch_process_signatures()