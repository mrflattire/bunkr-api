# src/bunkr_api/media/downloader.py
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TransferSpeedColumn,
)
from rich.prompt import Prompt

from ..config import DEFAULT_OUTPUT_DIR, HEADERS
from ..core.tokens import mint_single_url_async
from ..utils.formatting import (
    clean_dragged_path,
    get_album_folder_name,
    parse_selection,
    sanitize_filename_simple,
)

console = Console()

class DownloadEngine:
    def __init__(self, db):
        self.db = db
        self.active_processes_lock = threading.Lock()
        self.active_processes = {}  
        self.shutdown_event = threading.Event()

    def execute_ytdlp_task(self, index, total_files, asset_data, slot_id,
                          task_id, progress, output_root):
        """Assembles and executes yt-dlp with real-time progress parsing."""
        title = asset_data.get("title") or f"track_{index}"
        db_id = asset_data.get("db_asset_id")
        
        try:
            cdn_url = self.db.get_valid_url(db_id) if db_id else asset_data.get("signed_cdn_url")
        except Exception as e:
            progress.console.print(f"[red][-][/red] Token Error for {title}: {e}")
            cdn_url = None

        if not cdn_url:
            if db_id: self.db.update_download_status(db_id, "FAILED", error="No URL")
            return False

        if db_id: self.db.update_download_status(db_id, "DOWNLOADING")

        safe_name = sanitize_filename_simple(title)
        album_id = asset_data.get("album_id") or "0"
        album_title = asset_data.get("album_title") or "Unknown"
        
        album_dir = output_root / get_album_folder_name(album_id, album_title)
        album_dir.mkdir(parents=True, exist_ok=True)
        dest = album_dir / safe_name

        ytdlp_cmd = [
            "yt-dlp", "--no-playlist", "--newline", "--continue",
            "--retries", "50", "--fragment-retries", "10", "--retry-sleep", "5",
            "--referer", "https://bunkr.cr/",
            "--add-header", "Origin:https://bunkr.cr",
            "--add-header", f"User-Agent:{HEADERS['User-Agent']}",
            "--progress-template", "download:PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes)s",
            "-o", str(dest), cdn_url
        ]

        progress.update(task_id, description=f"[Worker {slot_id + 1}] {safe_name[:25]}", completed=0, total=None)

        try:
            c_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            proc = subprocess.Popen(
                ytdlp_cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True, 
                errors="replace",
                creationflags=c_flags
            )
            
            with self.active_processes_lock:
                self.active_processes[slot_id] = proc

            if proc.stdout:
                for line in proc.stdout:
                    if self.shutdown_event.is_set():
                        break
                    if line.startswith("PROGRESS "):
                        parts = line.split()
                        if len(parts) == 3:
                            with suppress(Exception):
                                progress.update(task_id, completed=int(parts[1]), total=int(parts[2]))

            proc.wait()
            
            if self.shutdown_event.is_set():
                if db_id: self.db.update_download_status(db_id, "PENDING")
                return False

            if proc.returncode == 0:
                progress.console.print(f"[green][+][/green] Finished: {safe_name}")
                if db_id: 
                    self.db.update_download_status(db_id, "COMPLETED", str(dest))
                    with self.db.connection() as conn:
                        conn.execute("UPDATE assets SET is_staged = 0 WHERE id = ?;", (db_id,))
                return True
            else:
                if db_id: self.db.update_download_status(db_id, "FAILED", error=f"Exit code {proc.returncode}")
                return False
                
        except Exception as e:
            if not self.shutdown_event.is_set() and db_id:
                self.db.update_download_status(db_id, "FAILED", error=str(e))
            return False
        finally:
            with self.active_processes_lock:
                self.active_processes.pop(slot_id, None)

    async def resolve_tokens_async(self, assets):
        """Ensures all assets have valid tokens before the thread pool begins."""
        from curl_cffi.requests import AsyncSession
        now = time.time()
        needed = [a for a in assets if not a.get("signed_cdn_url") or (a.get("token_expiry_timestamp") or 0) < now + 120]
        
        if not list(filter(lambda x: x.get('db_asset_id'), needed)):
            return
            
        console.print(f"[*] Pre-minting [cyan]{len(needed)}[/cyan] download tokens...")
        
        sem = asyncio.Semaphore(4)
        async def worker(session, a):
            async with sem:
                try:
                    fid = str(a.get("true_file_id") or a.get("slug_id"))
                    url = await mint_single_url_async(session, fid)
                    self.db.update_asset_url(a["db_asset_id"], url)
                except Exception:
                    pass
                
        async with AsyncSession(impersonate="chrome") as session:
            await asyncio.gather(*[worker(session, a) for a in needed])

    async def run(self, files_list, workers=1, output_dir=DEFAULT_OUTPUT_DIR):
        """
        The main execution loop for the engine.

        Async: awaits resolve_tokens_async() directly instead of wrapping it
        in a nested asyncio.run() (which crashes with "asyncio.run() cannot
        be called from a running event loop" whenever a caller — like the
        developer-facing BunkrAPI.download_album() — is itself already
        running inside an event loop). Worker tasks go through
        loop.run_in_executor(), which returns real asyncio.Futures, so
        awaiting asyncio.wait() on them yields control back to the event
        loop between polls instead of blocking it the way
        concurrent.futures.wait() did.
        """
        self.shutdown_event.clear()
        
        if not shutil.which("yt-dlp"):
            console.print("[red][-][/red] Error: 'yt-dlp' was not found in your system PATH.")
            return

        # 1. Await token prep directly — no nested asyncio.run()
        await self.resolve_tokens_async(files_list)

        # 2. Setup Progress
        progress = Progress(
            TextColumn("{task.description}"), BarColumn(bar_width=40),
            TaskProgressColumn(), DownloadColumn(), TransferSpeedColumn(),
            console=console, transient=True
        )

        # 3. Execution Pool
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=workers)
        interrupted = False
        try:
            with progress:
                slot_tasks = [progress.add_task(f"[Worker {i+1}] Idle", total=None) for i in range(workers)]
                slots = list(range(workers))
                task_iterator = enumerate(files_list, start=1)
                slot_for_future = {}
                pending = set()

                while True:
                    while slots:
                        try:
                            index, asset = next(task_iterator)
                            slot = slots.pop(0)
                            future = loop.run_in_executor(
                                executor, self.execute_ytdlp_task, index, len(files_list),
                                asset, slot, slot_tasks[slot], progress, output_dir
                            )
                            slot_for_future[future] = slot
                            pending.add(future)
                        except StopIteration:
                            break

                    if not pending:
                        break

                    done, pending = await asyncio.wait(
                        pending, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                    )

                    for future in done:
                        slot_freed = slot_for_future.pop(future)
                        slots.append(slot_freed)
                        progress.update(slot_tasks[slot_freed], description=f"[Worker {slot_freed+1}] Idle", completed=0, total=None)
                        exc = future.exception()
                        if exc:
                            console.print(f"[red][-][/red] Worker error: {exc}")

        except (KeyboardInterrupt, asyncio.CancelledError):
            # On modern Python (3.11+, and confirmed here on 3.14), Ctrl+C
            # under asyncio.run() does NOT raise KeyboardInterrupt directly
            # into the running coroutine — the Runner cancels the task
            # instead, which surfaces here as CancelledError at whatever
            # await point was active (in our case, inside asyncio.wait()).
            # A raw KeyboardInterrupt is only injected directly into
            # whatever's executing if the user presses Ctrl+C a second time
            # after that — which used to land mid-way through this method's
            # OWN finally-block shutdown call, corrupting it. Catching
            # CancelledError here means a single Ctrl+C is now enough to
            # trigger clean cleanup instead of needing a second, more
            # destructive one.
            interrupted = True
            self.shutdown_event.set()
            console.print("\n[bold yellow][!] Interrupt detected. Cleaning up...[/bold yellow]")
            with self.active_processes_lock:
                for proc in self.active_processes.values():
                    with suppress(Exception):
                        proc.terminate()
            executor.shutdown(wait=False, cancel_futures=True)
            await asyncio.sleep(1)
        finally:
            # Only do a full blocking join on the normal-completion path.
            # On the interrupted path we've already requested a fast,
            # non-blocking shutdown above (wait=False, cancel_futures=True)
            # — redundantly joining here too would undo that and reopen
            # the exact window where a second Ctrl+C used to land mid-join
            # and corrupt shutdown. On normal completion `pending` is
            # already empty by the time we get here, so this join is
            # near-instant anyway.
            if not interrupted:
                executor.shutdown(wait=True)

def prompt_for_inputs(db):
    """Restored: Interactive catalog browser from original download.py."""
    db_albums = []
    try:
        db_albums = db.get_all_albums() or []
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Warning: Could not query DB catalog: {e}")

    console.print()
    if db_albums:
        console.print("[bold magenta][*] Discovered Albums Cataloged in DB:[/bold magenta]")
        for idx, album in enumerate(db_albums, start=1):
            album_dict = dict(album)
            staged_flag = " [bold green][STAGED][/bold green]" if album_dict.get('is_staged') == 1 else ""
            console.print(f"  [cyan]{idx:2d}[/cyan] • [yellow]{album_dict['title']}[/yellow] ({album_dict['file_count']} items){staged_flag} [dim](DB ID: {album_dict['id']})[/dim]")
        console.print()

    console.print("[dim]Special keywords: 'staged' (all staged) | 'triage' (failed items)[/dim]")
    try:
        raw = Prompt.ask("[bold cyan][?][/bold cyan] Choose a record number, drop a fresh JSON path, or 'q' to exit").strip()
    except KeyboardInterrupt:
        sys.exit(0)

    if raw.lower() in ('q', 'quit', 'exit'):
        sys.exit(0)

    if raw.lower() == 'staged':
        return None, None, None, prompt_for_workers(), True, False
    if raw.lower() == 'triage':
        return None, None, None, prompt_for_workers(), False, True

    raw = clean_dragged_path(raw)
    input_path, db_id = None, None

    if raw.isdigit():
        num_val = int(raw)
        if db_albums and 1 <= num_val <= len(db_albums):
            db_id = db_albums[num_val - 1]["id"]
            console.print(f"[*] Resolved selection: [yellow]{db_albums[num_val - 1]['title']}[/yellow]")
        else:
            db_id = num_val
    else:
        candidate = Path(raw).expanduser()
        if candidate.exists() and candidate.is_file():
            input_path = candidate
        else:
            console.print("[bold red][-][/bold red] Selection not recognized.")
            sys.exit(1)

    selection = Prompt.ask("[bold cyan][?][/bold cyan] Enter item index, list, or range [dim](Enter for ALL)[/dim]").strip()
    if not selection:
        selection = 'all'
    
    workers = prompt_for_workers()
    return input_path, db_id, selection, workers, False, False

def prompt_for_workers() -> int:
    workers_input = Prompt.ask("[bold cyan][?][/bold cyan] Enter worker concurrency (MAX=5)", default="1").strip()
    try:
        return min(5, max(1, int(workers_input)))
    except ValueError:
        return 1

def main():
    """Standalone CLI entry point for 'bunkr-download'."""
    import argparse

    from ..core.db import DatabaseManager

    parser = argparse.ArgumentParser(description="Bunkr Standalone Downloader CLI")
    parser.add_argument('-i', '--input', type=str, help="Legacy JSON path")
    parser.add_argument('--db-id', type=int, help="Database ID for album to download")
    parser.add_argument('-w', '--workers', type=int, help="Worker concurrency")
    parser.add_argument('-n', '--number', type=str, help="Item/file selection")
    parser.add_argument('-o', '--output', type=str, help="Output directory")
    parser.add_argument('--staged', action='store_true', help="Download staged items")
    parser.add_argument('--triage', action='store_true', help="Download failed items")
    args = parser.parse_args()

    db = DatabaseManager()
    engine = DownloadEngine(db)
    
    input_json_path = args.input
    db_id = args.db_id
    selection_arg = args.number
    workers = args.workers
    run_staged = args.staged
    run_triage = args.triage

    # Interactive Fallback
    if not any([db_id, input_json_path, run_staged, run_triage]) and len(sys.argv) == 1:
        input_json_path, db_id, selection_arg, workers, run_staged, run_triage = prompt_for_inputs(db)

    if not workers: workers = 1
    
    files_list = []

    if run_staged:
        console.print("[bold cyan][*] Extracting all staged files...[/bold cyan]")
        assets = db.get_all_albums() # Dummy fetch to use connection
        with db.connection() as conn:
            rows = conn.execute("""
                SELECT a.*, al.title AS album_title FROM assets a
                LEFT JOIN albums al ON a.album_id = al.id
                WHERE a.is_staged = 1 OR al.is_staged = 1
                ORDER BY a.album_id, a.track_number ASC;
            """).fetchall()
            for r in rows:
                d = dict(r)
                d['db_asset_id'] = d['id']
                files_list.append(d)

    elif run_triage:
        console.print("[bold red][*] Auto-Triage: Retrying failed downloads...[/bold red]")
        with db.connection() as conn:
            rows = conn.execute("""
                SELECT a.*, al.title AS album_title FROM assets a
                LEFT JOIN albums al ON a.album_id = al.id
                WHERE a.download_status = 'FAILED'
                ORDER BY a.album_id, a.track_number ASC;
            """).fetchall()
            for r in rows:
                d = dict(r)
                d['db_asset_id'] = d['id']
                files_list.append(d)

    elif db_id:
        with db.connection() as conn:
            album = conn.execute("SELECT * FROM albums WHERE id=?", (db_id,)).fetchone()
            if not album:
                console.print(f"[red][!] Album {db_id} not found.[/red]")
                return
            assets = db.get_album_assets(db_id)
            for a in assets:
                d = dict(a)
                d['db_asset_id'] = d['id']
                d['album_title'] = album['title']
                d['album_id'] = album['id']
                files_list.append(d)

    elif input_json_path:
        with open(input_json_path, encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("selected_album", {})
        for item in data.get("files_found", []):
            files_list.append({
                "db_asset_id": None,
                "album_id": meta.get("album_index_number", "legacy"),
                "album_title": meta.get("title", "unknown"),
                "title": item.get("original") or item.get("title"),
                "signed_cdn_url": item.get("signed_cdn_url"),
                "true_file_id": item.get("true_file_id")
            })

    if not files_list:
        console.print("[yellow][!] No targets available.[/yellow]")
        return

    # Filter selection
    if selection_arg and selection_arg != 'all':
        try:
            indices = parse_selection(selection_arg, total_items=len(files_list))
            files_list = [files_list[i-1] for i in indices]
        except (ValueError, TypeError, IndexError) as e:
            console.print(f"[red][!] Selection error: {e}[/red]")
            return

    out_dir = Path(args.output).expanduser() if args.output else DEFAULT_OUTPUT_DIR
    try:
        asyncio.run(engine.run(files_list, workers=workers, output_dir=out_dir))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Safety net for an interrupt landing before run()'s own try block
        # starts (e.g. during the token pre-minting await) — that window
        # isn't covered by run()'s internal handler, so without this the
        # user would see a raw traceback instead of a clean exit.
        console.print("\n[bold yellow][!] Interrupted before downloads started.[/bold yellow]")

if __name__ == "__main__":
    main()