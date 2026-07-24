# src/album_manager/media/downloader.py
import os
import re
import sys
import time
import shutil
import asyncio
import threading
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

from rich.console import Console
from rich.progress import (
    Progress, TextColumn, BarColumn, TaskProgressColumn, 
    DownloadColumn, TransferSpeedColumn
)

# Internal Package Imports
from ..config import DEFAULT_OUTPUT_DIR, HEADERS
from ..utils.formatting import sanitize_filename_simple, get_album_folder_name
from ..core.tokens import mint_single_url_async

console = Console()

class DownloadEngine:
    def __init__(self, db):
        self.db = db
        self.active_processes_lock = threading.Lock()
        self.active_processes = {}  # slot_id -> Popen
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
        # Handle cases where asset_data keys might be slightly different
        album_id = asset_data.get("album_id") or "0"
        album_title = asset_data.get("album_title") or "Unknown"
        
        album_dir = output_root / get_album_folder_name(album_id, album_title)
        album_dir.mkdir(parents=True, exist_ok=True)
        dest = album_dir / safe_name

        ytdlp_cmd = [
            "yt-dlp", "--no-playlist", "--newline", "--continue",
            "--retries", "10", "--socket-timeout", "30",
            "--referer", HEADERS["Referer"],
            "--add-header", f"Origin:{HEADERS['Origin']}",
            "--add-header", f"User-Agent:{HEADERS['User-Agent']}",
            "--progress-template", "download:PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes)s",
            "-o", str(dest), cdn_url
        ]

        progress.update(task_id, description=f"[Worker {slot_id + 1}] {safe_name[:25]}", completed=0, total=None)

        try:
            # Set creationflags to handle signals properly on Windows
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

            for line in proc.stdout:
                if self.shutdown_event.is_set():
                    break
                if line.startswith("PROGRESS "):
                    parts = line.split()
                    if len(parts) == 3:
                        try:
                            progress.update(task_id, completed=int(parts[1]), total=int(parts[2]))
                        except: pass

            proc.wait()
            
            if self.shutdown_event.is_set():
                if db_id: self.db.update_download_status(db_id, "PENDING")
                return False

            if proc.returncode == 0:
                progress.console.print(f"[green][+][/green] Finished: {safe_name}")
                if db_id: self.db.update_download_status(db_id, "COMPLETED", str(dest))
                return True
            else:
                progress.console.print(f"[red][-] Failed: {safe_name} (Code {proc.returncode})[/red]")
                if db_id: self.db.update_download_status(db_id, "FAILED", error=f"Exit code {proc.returncode}")
                return False
                
        except Exception as e:
            if not self.shutdown_event.is_set():
                progress.console.print(f"[red][-][/red] Error {safe_name}: {e}")
                if db_id: self.db.update_download_status(db_id, "FAILED", error=str(e))
            return False
        finally:
            with self.active_processes_lock:
                self.active_processes.pop(slot_id, None)

    async def resolve_tokens_async(self, assets):
        """Ensures all assets have valid tokens before the thread pool begins."""
        from curl_cffi.requests import AsyncSession
        now = time.time()
        needed = [a for a in assets if not a.get("signed_cdn_url") or (a.get("token_expiry_timestamp") or 0) < now + 120]
        
        if not needed: return
        console.print(f"[*] Pre-minting [cyan]{len(needed)}[/cyan] download tokens...")
        
        sem = asyncio.Semaphore(4)
        async def worker(session, a):
            async with sem:
                try:
                    fid = str(a.get("true_file_id") or a.get("slug_id"))
                    url = await mint_single_url_async(session, fid)
                    self.db.update_asset_url(a["id"], url)
                except: pass
                
        async with AsyncSession(impersonate="chrome") as session:
            await asyncio.gather(*[worker(session, a) for a in needed])

    def run(self, files_list, workers=1, output_dir=DEFAULT_OUTPUT_DIR):
        """The main loop with graceful KeyboardInterrupt handling."""
        self.shutdown_event.clear()
        
        # 1. Async token prep
        loop_f = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
        if loop_f:
            asyncio.run(self.resolve_tokens_async(files_list), loop_factory=loop_f)
        else:
            asyncio.run(self.resolve_tokens_async(files_list))

        # 2. Setup Progress
        progress = Progress(
            TextColumn("{task.description}"), BarColumn(bar_width=40),
            TaskProgressColumn(), DownloadColumn(), TransferSpeedColumn(),
            console=console, transient=True
        )
        
        # 3. Execution Pool
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            with progress:
                slot_tasks = [progress.add_task(f"[Worker {i+1}] Idle", total=None) for i in range(workers)]
                futures = []
                slots = list(range(workers))
                task_iterator = iter(files_list)
                active_map = {} # future -> slot_id

                while True:
                    # Fill available slots
                    while slots:
                        try:
                            asset = next(task_iterator)
                            slot = slots.pop(0)
                            f = executor.submit(
                                self.execute_ytdlp_task, 0, len(files_list), 
                                asset, slot, slot_tasks[slot], progress, output_dir
                            )
                            futures.append(f)
                            active_map[f] = slot
                        except StopIteration:
                            break

                    if not futures:
                        break

                    # Wait for at least one to finish, timeout=1.0 allows Ctrl+C to break in
                    done, not_done = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
                    
                    for f in done:
                        futures.remove(f)
                        slot_freed = active_map.pop(f)
                        slots.append(slot_freed)
                        progress.update(slot_tasks[slot_freed], description=f"[Worker {slot_freed+1}] Idle", completed=0, total=None)

        except KeyboardInterrupt:
            self.shutdown_event.set()
            console.print("\n[bold yellow][!] Interrupt detected. Cleaning up processes...[/bold yellow]")
            
            # Kill all active yt-dlp subprocesses
            with self.active_processes_lock:
                for proc in self.active_processes.values():
                    try:
                        proc.terminate()
                    except: pass
            
            executor.shutdown(wait=False, cancel_futures=True)
            console.print("[bold green][+][/bold green] Cleanup complete. Returning to menu.")
            # Small delay to let the terminal clear the progress lines
            time.sleep(1)
        finally:
            executor.shutdown(wait=True)