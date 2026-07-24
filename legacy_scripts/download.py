# download.py
import json
import argparse
import subprocess
import shutil
import sys
import os
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from rich.prompt import Prompt
from rich.console import Console
from rich.progress import (
    Progress,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    DownloadColumn,
    TransferSpeedColumn,
)

# Core and utility imports
from core import DatabaseManager
from utils import format_bytes, clean_dragged_path

# Paths config
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "bunkr_downloads"

console = Console()
db = DatabaseManager()

# Live yt-dlp subprocess handles, so an interrupt can actually kill them
active_processes_lock = threading.Lock()
active_processes = {}  # slot_id -> Popen

shutdown_event = threading.Event()


def parse_arguments():
    """Parse command line arguments for the downloader script."""
    parser = argparse.ArgumentParser(description="Parallel/Sequential yt-dlp Asset Downloader Queue.")
    parser.add_argument(
        '-i', '--input',
        type=str,
        default=None,
        help="Path to the legacy album JSON file configuration."
    )
    parser.add_argument(
        '--db-id',
        type=int,
        default=None,
        help="The database row ID of the album to download."
    )
    parser.add_argument(
        '-o', '--output',
        type=str,
        default=None,
        help=f"Directory to save downloaded files (default: {DEFAULT_OUTPUT_DIR})."
    )
    parser.add_argument(
        '-w', '--workers',
        type=int,
        default=None,
        help="Number of concurrent download worker threads to spawn (Max: 5)."
    )
    parser.add_argument(
        '-n', '--number',
        type=str,
        default=None,
        help=(
            "Restrict the run to specific item(s) by their "
            "1-based position inside the selection. "
            "Accepts index ('5'), comma list ('3,7,12'), range ('10-20'), or mix ('1,4-6')."
        )
    )
    parser.add_argument(
        '--staged',
        action='store_true',
        help="Automatically process all assets or albums marked as staged in the database."
    )
    parser.add_argument(
        '--triage',
        action='store_true',
        help="Automatically process all assets currently marked as FAILED in the database."
    )
    return parser.parse_args()


def parse_selection(spec: str, total_files: int) -> set:
    """Parses a -n/--number spec like '1,4-6,9' into a set of 1-based indices."""
    selected = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_str, end_str = chunk.split("-", 1)
            start, end = int(start_str), int(end_str)
            if start > end:
                start, end = end, start
            selected.update(range(start, end + 1))
        else:
            selected.add(int(chunk))

    out_of_range = {i for i in selected if i < 1 or i > total_files}
    if out_of_range:
        raise ValueError(
            f"Index/indices {sorted(out_of_range)} out of range "
            f"(Target has {total_files} items, valid range is 1-{total_files})."
        )
    return selected


def prompt_for_inputs():
    """
    Scans the tracking database to display cataloged albums, lists local JSON files
    as legacy fallbacks, and prompts for DB ID, selection number, or physical path.
    """
    db_albums = []
    try:
        db_albums = db.get_all_albums() or []
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Warning: Could not query DB catalog: {e}")

    console.print()
    if db_albums:
        console.print("[bold magenta][*] Discovered Albums Cataloged in DB:[/bold magenta]")
        for idx, album in enumerate(db_albums, start=1):
            # Safe type conversion to plain dict to avoid sqlite3.Row AttributeErrors
            album_dict = dict(album)
            is_staged_flag = " [bold green][STAGED][/bold green]" if album_dict.get('is_staged') == 1 else ""
            console.print(f"  [cyan]{idx:2d}[/cyan] • [yellow]{album_dict['title']}[/yellow] ({album_dict['file_count']} items){is_staged_flag} [dim](DB ID: {album_dict['id']})[/dim]")
        console.print()

    console.print("[dim]Special keywords: 'staged' (pulls all staged items) | 'triage' (retries failed items)[/dim]")
    try:
        raw = Prompt.ask("[bold cyan][?][/bold cyan] Choose a record number, drop a fresh JSON path, or 'q' to exit").strip()
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Session cancelled.")
        sys.exit(0)

    if raw.lower() in ('q', 'quit', 'exit'):
        console.print("[bold yellow][!][/bold yellow] Execution exited by user.")
        sys.exit(0)

    if raw.lower() == 'staged':
        return None, None, None, prompt_for_workers(), True, False
    if raw.lower() == 'triage':
        return None, None, None, prompt_for_workers(), False, True

    raw = clean_dragged_path(raw)

    input_path = None
    db_id = None

    if raw.isdigit():
        num_val = int(raw)
        if db_albums and 1 <= num_val <= len(db_albums):
            db_id = db_albums[num_val - 1]["id"]
            console.print(f"[*] Resolved selection to: [yellow]{db_albums[num_val - 1]['title']}[/yellow] (DB ID: {db_id})")
        else:
            db_id = num_val
    else:
        candidate = Path(raw).expanduser()
        if candidate.exists() and candidate.is_file():
            input_path = candidate
        else:
            console.print("[bold red][-][/bold red] Selection not recognized. Exiting downloader pipeline.")
            sys.exit(1)

    try:
        selection = Prompt.ask(
            "[bold cyan][?][/bold cyan] Enter item index, list, or range [dim](e.g. 5 | 3,7,12 | Enter for ALL)[/dim]"
        ).strip()
        if selection.lower() in ('q', 'quit', 'exit'):
            sys.exit(0)
        if not selection or selection.lower() == 'all':
            selection = None

        workers = prompt_for_workers()
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Session cancelled.")
        sys.exit(0)

    return input_path, db_id, selection, workers, False, False


def prompt_for_workers() -> int:
    """Helper snippet to abstract worker concurrency gathering."""
    try:
        workers_input = Prompt.ask(
            "[bold cyan][?][/bold cyan] Enter worker concurrency (MAX=5) [dim](Press Enter for default)[/dim]"
        ).strip()
        if workers_input.lower() in ('q', 'quit', 'exit'):
            sys.exit(0)
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Session cancelled.")
        sys.exit(0)

    if workers_input:
        try:
            return max(1, int(workers_input))
        except ValueError:
            console.print("[bold yellow][!][/bold yellow] Invalid configuration. Defaulting to 1 worker.")
            return 1
    return 1


def sanitize_filename(name: str) -> str:
    """Removes or replaces characters that are illegal in Windows/Linux file systems."""
    return "".join(c for c in name if c.isalnum() or c in "._- ()").strip()


def get_album_folder_name(album_id, album_title: str) -> str:
    """
    Builds the '#{id}_{title}' subfolder name each album's files land in,
    e.g. '#11_Some_Album_Title'. album_id can be an int (normal DB-backed
    downloads) or a string fallback (legacy JSON path with no DB row) —
    str() handles both uniformly.
    """
    clean = re.sub(r'[\\/*?:"<>|]', "", album_title or "unknown_album")
    clean = re.sub(r'\s+', "_", clean).strip("_") or "unknown_album"
    return f"#{album_id}_{clean}"


def execute_ytdlp_task(index: int, total_files: int, asset_data: dict, slot_id: int,
                        task_id, progress: Progress):
    """Assembles and executes the yt-dlp subprocess payload with real-time feedback loop."""
    title = asset_data.get("title") or f"track_{index}"
    db_id = asset_data.get("db_asset_id")
    
    if db_id:
        try:
            cdn_url = db.get_valid_url(db_id)
        except Exception as e:
            progress.console.print(f"[red][-][/red] Failed to grab valid token for ID {db_id}: {e}")
            cdn_url = None
    else:
        cdn_url = asset_data.get("signed_cdn_url")

    safe_name = sanitize_filename(title)
    album_folder = get_album_folder_name(
        asset_data.get("album_id", "unsorted"),
        asset_data.get("album_title", "unsorted")
    )
    album_dir = DEFAULT_OUTPUT_DIR / album_folder
    album_dir.mkdir(parents=True, exist_ok=True)
    dest = album_dir / safe_name

    def set_idle():
        progress.update(task_id, description=f"[Worker {slot_id + 1}] Idle...", completed=0, total=None)

    if not cdn_url:
        progress.console.print(f"[red][-][/red] [{index}/{total_files}] Skipping: {safe_name} (URL missing)")
        if db_id:
            db.update_download_status(db_id, "FAILED", error="Missing signed CDN URL")
        set_idle()
        return False

    if db_id:
        db.update_download_status(db_id, "DOWNLOADING")

    ytdlp_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--retries", "50",
        "--fragment-retries", "10",
        "--retry-sleep", "5",
        "--concurrent-fragments", "1",
        "--socket-timeout", "25",
        "--continue",
        "--newline",
        "--referer", "https://bunkr.cr/",
        "--add-header", "Origin:https://bunkr.cr",
        "--add-header", (
            "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "--add-header", "Accept-Encoding:identity",
        "--progress-template", "download:PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes)s",
        "-o", str(dest),
        cdn_url,
    ]

    progress.update(task_id, description=f"[Worker {slot_id + 1}] Task {index}/{total_files}: {safe_name}",
                     completed=0, total=None)

    env_config = os.environ.copy()
    env_config["PYTHONIOENCODING"] = "utf-8"

    process = None
    try:
        process = subprocess.Popen(
            ytdlp_cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            encoding="utf-8",
            errors="replace",
            env=env_config
        )
        with active_processes_lock:
            active_processes[slot_id] = process

        if process.stdout:
            for line in process.stdout:
                if shutdown_event.is_set():
                    break
                clean_line = line.strip()
                if clean_line.startswith("PROGRESS "):
                    parts = clean_line.split()
                    if len(parts) == 3:
                        _, downloaded_str, total_str = parts
                        try:
                            downloaded_bytes = int(downloaded_str)
                            total_bytes = int(total_str)
                            progress.update(task_id, completed=downloaded_bytes, total=total_bytes)
                        except ValueError:
                            pass

        if shutdown_event.is_set():
            if db_id:
                db.update_download_status(db_id, "PENDING")
            return False

        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ytdlp_cmd)

        progress.console.print(f"[green][+][/green] [{index}/{total_files}] Successfully downloaded: {safe_name}")
        
        if db_id:
            db.update_download_status(db_id, "COMPLETED", local_path=str(dest))
            with db.connection() as conn:
                conn.execute("UPDATE assets SET is_staged = 0 WHERE id = ?;", (db_id,))
            
        set_idle()
        return True

    except subprocess.CalledProcessError as err:
        progress.console.print(f"[red][-][/red] [{index}/{total_files}] Failed: {safe_name} (Code {err.returncode})")
        if db_id:
            db.update_download_status(db_id, "FAILED", error=f"yt-dlp exited with code {err.returncode}")
        set_idle()
        return False
    except Exception as e:
        if not shutdown_event.is_set():
            progress.console.print(f"[red][-][/red] [{index}/{total_files}] Error: {e}")
            if db_id:
                db.update_download_status(db_id, "FAILED", error=str(e))
            set_idle()
        return False
    finally:
        with active_processes_lock:
            active_processes.pop(slot_id, None)


async def resolve_download_tokens_async(indexed_files: list):
    """Parallel pre-minting token refresh sequence."""
    from mint import mint_single_url_async, AsyncSession
    import asyncio

    now = int(time.time())
    needed = []

    db_ids = [item.get("db_asset_id") for _, item in indexed_files if item.get("db_asset_id")]
    if not db_ids:
        return

    with db.connection() as conn:
        placeholders = ", ".join("?" for _ in db_ids)
        rows = conn.execute(
            f"SELECT id, true_file_id, source_url, signed_cdn_url, token_expiry_timestamp "
            f"FROM assets WHERE id IN ({placeholders});",
            db_ids
        ).fetchall()

    for asset_row in rows:
        expiry = asset_row["token_expiry_timestamp"]
        if not asset_row["signed_cdn_url"] or not expiry or expiry <= now + 120:
            needed.append(dict(asset_row))

    if not needed:
        return

    console.print(f"[*] [Concurrency Engine] Parallel pre-minting triggered for [cyan]{len(needed)}[/cyan] download queue token(s)...")

    max_workers = int(db.get_config_val("max_workers", "4"))
    sem = asyncio.Semaphore(max_workers)

    async def worker(session, asset_dict):
        async with sem:
            raw_id = asset_dict.get("true_file_id")
            file_id = str(raw_id).strip() if raw_id is not None else None
            
            if not file_id and asset_dict.get("source_url"):
                import re, urllib.parse
                source_url = asset_dict["source_url"]
                match = re.search(r'/f/([^./?]+)', source_url)
                if match:
                    file_id = match.group(1)
                else:
                    parsed_path = urllib.parse.urlparse(source_url).path
                    file_id, _ = os.path.splitext(os.path.basename(parsed_path.rstrip("/")))

            if not file_id:
                return

            try:
                fresh_url = await mint_single_url_async(session, file_id)
                db.update_asset_url(asset_dict["id"], fresh_url)
            except Exception:
                pass

    async with AsyncSession(impersonate="chrome") as session:
        tasks = [worker(session, asset) for asset in needed]
        await asyncio.gather(*tasks)


def download_assets():
    global DEFAULT_OUTPUT_DIR
    input_json_path = None
    db_id = None
    selection_arg = None
    workers = 1
    run_staged = False
    run_triage = False

    if len(sys.argv) == 1:
        input_json_path, db_id, selection_arg, workers, run_staged, run_triage = prompt_for_inputs()
    else:
        args = parse_arguments()

        if args.output:
            DEFAULT_OUTPUT_DIR = Path(clean_dragged_path(args.output)).expanduser()

        db_id = args.db_id
        run_staged = args.staged
        run_triage = args.triage
        selection_arg = args.number
        workers = max(1, args.workers if args.workers is not None else 1)

        if not db_id and not run_staged and not run_triage:
            if args.input:
                input_json_path = Path(clean_dragged_path(args.input)).expanduser()
                if not input_json_path.exists():
                    console.print(f"[red][-][/red] Error: '{input_json_path}' not found.")
                    return
            else:
                try:
                    raw_p = Prompt.ask("[bold cyan]Path to the album JSON file or 'q' to exit[/bold cyan]")
                    if raw_p.lower() in ('q', 'quit', 'exit'):
                        sys.exit(0)
                    input_json_path = Path(clean_dragged_path(raw_p)).expanduser()
                except KeyboardInterrupt:
                    console.print("\n[bold yellow][!][/bold yellow] Canceled.")
                    sys.exit(0)

    MAX_WORKERS = 5
    if workers > MAX_WORKERS:
        console.print(f"[*] Requested {workers} workers, but the hard cap is {MAX_WORKERS}. Clamping down.")
        workers = MAX_WORKERS

    if not shutil.which("yt-dlp"):
        console.print("[red][-][/red] Error: 'yt-dlp' was not found in your system PATH.")
        return

    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files_list = []

    if run_staged:
        console.print("[bold cyan][*] Extracting all active staged files across database queues...[/bold cyan]")
        try:
            with db.connection() as conn:
                assets = conn.execute("""
                    SELECT a.*, al.title AS album_title FROM assets a
                    LEFT JOIN albums al ON a.album_id = al.id
                    WHERE a.is_staged = 1 OR al.is_staged = 1
                    ORDER BY a.album_id, a.track_number ASC;
                """).fetchall()
                for asset in assets:
                    files_list.append({
                        "db_asset_id": asset["id"],
                        "album_id": asset["album_id"],
                        "album_title": asset["album_title"],
                        "title": asset["title"],
                        "original": asset["original_filename"],
                        "signed_cdn_url": asset["signed_cdn_url"],
                        "size": asset["raw_size_bytes"]
                    })
        except Exception as e:
            console.print(f"[bold red][-][/bold red] Database query failed: {e}")
            return

    elif run_triage:
        console.print("[bold red][*] Auto-Triage: Aggregating all broken or FAILED download tracks...[/bold red]")
        try:
            with db.connection() as conn:
                assets = conn.execute("""
                    SELECT a.*, al.title AS album_title FROM assets a
                    LEFT JOIN albums al ON a.album_id = al.id
                    WHERE a.download_status = 'FAILED'
                    ORDER BY a.album_id, a.track_number ASC;
                """).fetchall()
                for asset in assets:
                    files_list.append({
                        "db_asset_id": asset["id"],
                        "album_id": asset["album_id"],
                        "album_title": asset["album_title"],
                        "title": asset["title"],
                        "original": asset["original_filename"],
                        "signed_cdn_url": asset["signed_cdn_url"],
                        "size": asset["raw_size_bytes"]
                    })
        except Exception as e:
            console.print(f"[bold red][-][/bold red] Database query failed: {e}")
            return

    elif db_id:
        console.print(f"[*] Querying tracking database records for Album ID: {db_id}...")
        try:
            with db.connection() as conn:
                album = conn.execute("SELECT * FROM albums WHERE id = ?;", (db_id,)).fetchone()
                if not album:
                    console.print(f"[bold red][-][/bold red] Database Album ID #{db_id} does not exist.")
                    return
                
                assets = conn.execute("SELECT * FROM assets WHERE album_id = ? ORDER BY track_number ASC;", (db_id,)).fetchall()
                for asset in assets:
                    files_list.append({
                        "db_asset_id": asset["id"],
                        "album_id": album["id"],
                        "album_title": album["title"],
                        "title": asset["title"],
                        "original": asset["original_filename"],
                        "signed_cdn_url": asset["signed_cdn_url"],
                        "size": asset["raw_size_bytes"]
                    })
        except Exception as e:
            console.print(f"[bold red][-][/bold red] Database query failed: {e}")
            return
    else:
        console.print(f"[*] Reading legacy fallback catalog from {input_json_path}...")
        with open(input_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        files_list = data.get("files_found", [])

        # No DB row backs this path, so there's no real album id — fall back
        # to whatever numeric label the scrape JSON itself carried.
        album_meta = data.get("selected_album", {})
        legacy_album_id = album_meta.get("album_index_number", "legacy")
        legacy_album_title = album_meta.get("title", "unknown_album")
        for item in files_list:
            item.setdefault("album_id", legacy_album_id)
            item.setdefault("album_title", legacy_album_title)

    if not files_list:
        console.print("[yellow][!] No targets available to download.[/yellow]")
        return

    total_files = len(files_list)
    indexed_files = list(enumerate(files_list, start=1))

    if selection_arg and not (run_staged or run_triage):
        try:
            selection = parse_selection(selection_arg, total_files)
        except ValueError as exc:
            console.print(f"[red][-][/red] Error: {exc}")
            return
        indexed_files = [(idx, item) for idx, item in indexed_files if idx in selection]

    if not indexed_files:
        console.print("[red][-][/red] No items match the selection.")
        return

    if db_id or run_staged or run_triage:
        import asyncio
        if sys.platform == 'win32':
            asyncio.run(resolve_download_tokens_async(indexed_files), loop_factory=asyncio.SelectorEventLoop)
        else:
            asyncio.run(resolve_download_tokens_async(indexed_files))

    queue_size = len(indexed_files)
    workers = min(workers, queue_size)

    console.print(f"[+] Found {total_files} potential items; preparing to download {queue_size} (Workers: {workers}).\n")

    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(bar_width=40, style="grey35", complete_style="cyan", finished_style="green"),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    )

    executor = ThreadPoolExecutor(max_workers=workers)
    available_slots = list(range(workers))
    slot_assignments = {}

    with progress:
        slot_task_ids = [
            progress.add_task(f"[Worker {i + 1}] Idle...", total=None, completed=0)
            for i in range(workers)
        ]

        try:
            task_iterator = iter(indexed_files)
            active_futures = []

            while True:
                while available_slots and (file_data := next(task_iterator, None)):
                    idx, item = file_data
                    slot = available_slots.pop(0)

                    f = executor.submit(
                        execute_ytdlp_task, idx, total_files, item, slot,
                        slot_task_ids[slot], progress
                    )
                    slot_assignments[f] = slot
                    active_futures.append(f)

                if not active_futures:
                    break

                done_batch = []
                for f in as_completed(active_futures):
                    done_batch.append(f)
                    break

                for f in done_batch:
                    active_futures.remove(f)
                    slot_freed = slot_assignments.pop(f)
                    available_slots.append(slot_freed)
                    try:
                        f.result()
                    except Exception:
                        pass

        except KeyboardInterrupt:
            shutdown_event.set()
            executor.shutdown(wait=False, cancel_futures=True)
            with active_processes_lock:
                interrupted_slots = list(active_processes.items())
            for _, proc in interrupted_slots:
                try:
                    proc.terminate()
                except Exception:
                    pass
            for _, proc in interrupted_slots:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            for slot_id, _ in interrupted_slots:
                progress.update(
                    slot_task_ids[slot_id],
                    description=f"[Worker {slot_id + 1}] Interrupted",
                    completed=0, total=None,
                )

            progress.stop()
            console.print("\n[yellow][![/yellow] Ctrl+C detected! Safely canceling download pipeline processes...")
            sys.exit(130)
        finally:
            if not shutdown_event.is_set():
                executor.shutdown(wait=True)

    console.print("\n[+] All download queue tasks processed.")


if __name__ == "__main__":
    if os.name == 'nt':
        subprocess.run("", shell=True)

    try:
        download_assets()
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Session canceled gracefully.")
        sys.exit(0)