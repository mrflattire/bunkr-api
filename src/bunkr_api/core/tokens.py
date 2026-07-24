import os
import sys
import time
import re
import asyncio
import urllib.parse
from curl_cffi.requests import AsyncSession
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

# Internal Package Imports
from ..config import HEADERS
from ..utils.http import execute_request_with_retry_async

console = Console()

async def mint_single_url_async(session, file_id: str) -> str:
    """
    Asynchronously resolves a true_file_id and fetches a fresh signed CDN URL.
    This is the core network logic.
    """
    if not file_id or file_id == "None":
        raise ValueError("Invalid or empty file ID provided.")

    mint_headers = HEADERS.copy()
    mint_headers.update({
        "Content-Type": "application/json",
        "Origin": "https://dl.bunkr.cr",
        "Referer": f"https://dl.bunkr.cr/file/{file_id}",
    })
    
    # Step 1: Query Metadata API
    meta_url = "https://dl.bunkr.cr/api/_001_v2"
    payload = {"id": str(file_id)}
    meta_res = await execute_request_with_retry_async(session, meta_url, method="POST", json_payload=payload, headers=mint_headers)
    meta_data = meta_res.json()
    
    cdn_host = meta_data.get("mediafiles")
    storage_path = meta_data.get("path")
    original_name = meta_data.get("original")

    if not all([cdn_host, storage_path, original_name]):
        raise ValueError(f"API response missing data for ID {file_id}")

    # Step 2: Request Dynamic Validation Token
    encoded_path = urllib.parse.quote(storage_path)
    sign_url = f"https://glb-apisign.cdn.cr/sign?path={encoded_path}"
    sign_res = await execute_request_with_retry_async(session, sign_url, method="GET", headers=mint_headers)
    sign_data = sign_res.json()
    
    token = sign_data.get("token")
    ex = sign_data.get("ex")

    # Step 3: URL Stitching
    encoded_name = urllib.parse.quote(original_name)
    return f"{cdn_host}{storage_path}?n={encoded_name}&token={token}&ex={ex}"


def mint_now(file_id: str) -> str:
    """
    Synchronous wrapper to run the async minter.
    Used as an escape hatch by the Database Manager.
    """
    async def _run_single():
        async with AsyncSession(impersonate="chrome") as session:
            return await mint_single_url_async(session, file_id)

    loop_factory = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
    if loop_factory:
        return asyncio.run(_run_single(), loop_factory=loop_factory)
    else:
        return asyncio.run(_run_single())


async def process_asset_task(session, db, sem, asset, progress, task_id):
    """
    Worker task that processes a single asset token refresh in a batch.
    """
    async with sem:
        # Resolve ID from various possible keys
        raw_id = asset.get("true_file_id") or asset.get("slug_id")
        file_id = str(raw_id).strip() if raw_id is not None else None
        
        # Fallback Parsing from source URL if ID is missing
        if not file_id and asset.get("source_url"):
            source_url = asset["source_url"]
            match = re.search(r'/f/([^./?]+)', source_url)
            if match:
                file_id = match.group(1)
            else:
                parsed_path = urllib.parse.urlparse(source_url).path
                file_id = os.path.basename(parsed_path.rstrip("/"))

        if not file_id:
            progress.advance(task_id)
            return

        try:
            fresh_url = await mint_single_url_async(session, file_id)
            db.update_asset_url(asset["id"], fresh_url)
        except Exception:
            pass
        finally:
            progress.advance(task_id)


async def refresh_all_tokens_async(db, assets, max_workers: int):
    """
    Batch-processes a list of assets concurrently using a semaphore.
    """
    sem = asyncio.Semaphore(max_workers)
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40, finished_style="green"),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task_id = progress.add_task(
            f"[cyan]Refreshing {len(assets)} tokens...", 
            total=len(assets)
        )
        
        async with AsyncSession(impersonate="chrome") as session:
            tasks = [
                process_asset_task(session, db, sem, asset, progress, task_id)
                for asset in assets
            ]
            await asyncio.gather(*tasks)


def daemon_loop(album_id: int = None):
    """
    Restored: Loop that polls the database and renews expiring tokens.
    If album_id is specified, it performs a one-shot targeted refresh.
    """
    from .db import DatabaseManager
    
    db = DatabaseManager()
    max_workers = int(db.get_config_val("max_workers", "4"))
    
    if album_id:
        console.print(f"[bold green][+][/bold green] Targeted Refresh for Album ID: [bold cyan]{album_id}[/bold cyan]")
    else:
        console.print("[bold green][+][/bold green] Token Minter Background Daemon active.")
        console.print("[dim]Monitoring database... Press Ctrl+C to stop.[/dim]")
    
    while True:
        try:
            # Query DB for assets that are NULL or expiring soon
            raw_assets = db.get_needs_refresh(album_id=album_id)
            expiring_assets = [dict(row) for row in raw_assets]
            
            if expiring_assets:
                loop_f = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
                if loop_f:
                    asyncio.run(refresh_all_tokens_async(db, expiring_assets, max_workers), loop_factory=loop_f)
                else:
                    asyncio.run(refresh_all_tokens_async(db, expiring_assets, max_workers))
            
            # If we were targeting a specific album, exit after one pass
            if album_id:
                console.print("[bold green][+][/bold green] Targeted refresh complete.")
                break
                
            # Otherwise, wait for the poll interval
            poll_interval = int(db.get_config_val("minter_poll_interval_seconds", "60"))
            time.sleep(poll_interval)
            
        except KeyboardInterrupt:
            console.print("\n[yellow][!][/yellow] Minter shut down.")
            break
        except Exception as e:
            console.print(f"[bold red][x] Minter Error:[/bold red] {e}")
            if album_id:
                break
            time.sleep(10)