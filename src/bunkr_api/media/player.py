import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path

from curl_cffi.requests.errors import CurlError
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.prompt import Prompt

from ..core.tokens import mint_single_url_async
from ..utils.formatting import clean_dragged_path, parse_selection

console = Console()

class PlayerEngine:
    def __init__(self, db):
        self.db = db

    def connect_to_ipc(self, ipc_path: str):
        """Establishes connection to MPV IPC (Named Pipe on Win, Unix Socket on Linux)."""
        if os.name == 'nt':
            try:
                return open(ipc_path, 'r+b', buffering=0)
            except OSError as e:
                raise ConnectionError(f"Failed to open Windows named pipe {ipc_path}: {e}") from e
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(ipc_path)
            return sock.makefile("rw", encoding="utf-8")

    def poll_mpv_status(self, ipc_path: str, stop_event: threading.Event, total_tracks: int):
        """Restored: Full Cache Syncing and Subscribes to MPV properties."""
        sock_file = None
        for _ in range(50):
            if stop_event.is_set(): return
            if os.name == 'nt' or os.path.exists(ipc_path):
                try:
                    sock_file = self.connect_to_ipc(ipc_path)
                    break
                except Exception:
                    pass
            time.sleep(0.1)

        if not sock_file: return

        # State keys must match observed property names exactly
        state = {
            "media-title": None, 
            "time-pos": None, 
            "duration": None, 
            "percent-pos": None, 
            "demuxer-cache-duration": None, # RESTORED: Exact property name
            "playlist-pos": None
        }
        
        observed = {
            1: "media-title", 
            2: "time-pos", 
            3: "duration", 
            4: "percent-pos", 
            5: "demuxer-cache-duration", # RESTORED: Cache syncing
            6: "playlist-pos"
        }

        try:
            for obs_id, prop_name in observed.items():
                payload = json.dumps({"command": ["observe_property", obs_id, prop_name]}) + "\n"
                if os.name == 'nt': sock_file.write(payload.encode('utf-8'))
                else: sock_file.write(payload)
            if os.name != 'nt': sock_file.flush()
        except Exception: 
            return

        progress_bar = Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.fields[time_pos]} / {task.fields[duration]})"),
            TextColumn("[yellow]Cache: {task.fields[cache_duration]}s"), # RESTORED: Field mapping
        )
        
        track_task = progress_bar.add_task(
            "Buffering...", 
            total=100, 
            time_pos="00:00", 
            duration="00:00", 
            cache_duration="0.0"
        )

        def fmt_time(s):
            if s is None: return "00:00"
            return f"{int(s)//60:02d}:{int(s)%60:02d}"

        with Live(Panel(progress_bar, title="[bold green]Live Stream Player Status[/bold green]", border_style="green"), refresh_per_second=10):
            while not stop_event.is_set():
                try:
                    if os.name == 'nt':
                        raw_line = sock_file.readline()
                        if not raw_line: break
                        line = raw_line.decode('utf-8', errors='ignore').strip()
                    else:
                        line = sock_file.readline().strip()
                        if not line: break

                    data = json.loads(line)
                    if data.get("event") == "property-change":
                        obs_id = data.get("id")
                        prop_name = observed.get(obs_id)
                        if prop_name:
                            state[prop_name] = data.get("data")
                        
                        # UI Update Logic
                        t_prefix = f"[{int(state['playlist-pos'] or 0)+1}/{total_tracks}] " if state['playlist-pos'] is not None else ""
                        cache_val = state['demuxer-cache-duration']
                        
                        progress_bar.update(
                            track_task, 
                            description=f"Playing: {t_prefix}{state['media-title'] or '...'}"[:45],
                            completed=int(state['percent-pos'] or 0), 
                            time_pos=fmt_time(state['time-pos']), 
                            duration=fmt_time(state['duration']),
                            cache_duration=f"{cache_val:.1f}" if cache_val is not None else "0.0" # RESTORED: precision float
                        )
                except Exception as e: 
                    console.print(f"[dim red]IPC polling read error: {e}[/dim red]")
                    continue
            with suppress(OSError, AttributeError):
                sock_file.close()
        
    async def resolve_tokens_async(self, assets):
        """Parallel token refresh for streaming with NULL safety."""
        from curl_cffi.requests import AsyncSession
        now = time.time()
        
        needed = []
        for a in assets:
            asset_dict = dict(a)
            url = asset_dict.get("signed_cdn_url")
            expiry = asset_dict.get("token_expiry_timestamp")
            if not url or expiry is None or expiry < (now + 60):
                needed.append(asset_dict)
        
        if not needed: return

        console.print(f"[bold yellow][*][/bold yellow] [Escape Hatch] Concurrent batch-refresh triggered for [cyan]{len(needed)}[/cyan] asset(s)...")

        max_workers = int(self.db.get_config_val("max_workers", "4"))
        sem = asyncio.Semaphore(max_workers)
        
        async def worker(session, a):
            async with sem:
                try:
                    fid = str(a.get("true_file_id") or a.get("slug_id"))
                    url = await mint_single_url_async(session, fid)
                    self.db.update_asset_url(a["id"], url)
                except (TimeoutError, CurlError, sqlite3.Error, KeyError, TypeError, AttributeError) as e:
                    console.print(f"[dim red]Minting failed for {a.get('title')[:20]}: {e}[/dim red]")
        
        async with AsyncSession(impersonate="chrome") as session:
            await asyncio.gather(*[worker(session, a) for a in needed])

    def play_mpv(self, playback_queue):
        """Assembles M3U and launches MPV with IPC polling."""
        console.print(f"\n[bold green][*][/bold green] Assembling Playlist ([cyan]{len(playback_queue)} tracks[/cyan])...")
        
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u", mode="w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for idx, title, url in playback_queue:
                    f.write(f"#EXTINF:-1,{idx}. {title}\n{url}\n")
                p_path = f.name
        except Exception as e: 
            console.print(f"[bold red][-][/bold red] Failed to generate temporary play-queue file: {e}")
            return

        ipc = rf"\\.\pipe\mpv_{os.getpid()}" if os.name == 'nt' else os.path.join(tempfile.gettempdir(), f"mpv_{os.getpid()}")
        stop_event = threading.Event()
        poll_thread = threading.Thread(target=self.poll_mpv_status, args=(ipc, stop_event, len(playback_queue)), daemon=True)

        console.print("[bold green][*][/bold green] Launching MPV engine with IPC control...")

        try:
            proc = subprocess.Popen(["mpv", "--force-window", f"--input-ipc-server={ipc}", "--idle=once", p_path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            poll_thread.start()
            proc.wait()
        except FileNotFoundError:
            console.print("[bold red][-][/bold red] Error: 'mpv' executable not found on system PATH.")
        except KeyboardInterrupt:
            console.print("\n[bold yellow][!][/bold yellow] Playback interrupted by user.")
            if 'proc' in locals(): proc.terminate()
        finally:
            stop_event.set()
            if os.path.exists(p_path): os.unlink(p_path)
            console.print("[bold green][+][/bold green] Player session closed safely.")

    def play_vlc(self, playback_queue):
        """VLC handoff logic."""
        console.print(f"\n[bold green][*][/bold green] Assembling Playlist ([cyan]{len(playback_queue)} tracks[/cyan])...")
        
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u", mode="w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for idx, title, url in playback_queue:
                    f.write(f"#EXTINF:-1,{idx}. {title}\n{url}\n")
                p_path = f.name
        except (OSError, ValueError, TypeError) as e:
            console.print(f"[bold red][-][/bold red] Failed to generate temporary play-queue file: {e}")
            return
        
        vlc = "vlc"
        if os.name == 'nt' and not shutil.which("vlc"):
            vlc = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
            
        console.print("[bold green][*][/bold green] Streaming via VLC... close the window to return.")
        try:
            subprocess.run([vlc, p_path], check=False)
        finally:
            if os.path.exists(p_path): os.unlink(p_path)
            console.print("[bold green][+][/bold green] Player session closed safely.")

def prompt_for_inputs(db):
    """Interactive catalog browser."""
    db_albums = []
    try:
        db_albums = db.get_all_albums() or []
    except (sqlite3.Error, KeyError, AttributeError) as e:
        console.print(f"[bold red][-][/bold red] Warning: Could not query DB catalog: {e}")

    console.print()
    if db_albums:
        console.print("[bold magenta][*] Discovered Albums Cataloged in DB:[/bold magenta]")
        for idx, album in enumerate(db_albums, start=1):
            album_dict = dict(album)
            is_staged_flag = " [bold green][STAGED][/bold green]" if album_dict.get('is_staged') == 1 else ""
            console.print(f"  [cyan]{idx:2d}[/cyan] • [yellow]{album_dict['title']}[/yellow] ({album_dict['file_count']} items){is_staged_flag} [dim](DB ID: {album_dict['id']})[/dim]")
        console.print()

    console.print("[dim]Special keywords: 'staged' (pulls all staged items)[/dim]")
    try:
        raw = Prompt.ask("[bold cyan][?][/bold cyan] Choose a record number, drop a fresh JSON path, or 'q' to exit").strip()
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Session cancelled.")
        sys.exit(0)

    if raw.lower() in ('q', 'quit', 'exit'):
        sys.exit(0)

    if raw.lower() == 'staged':
        player = Prompt.ask("[bold cyan][?][/bold cyan] Select Media Player Engine", choices=["mpv", "vlc"], default="mpv")
        return None, None, 'all', player, True

    raw = clean_dragged_path(raw)
    input_path, db_id = None, None

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
            console.print("[bold red][-][/bold red] Error: Selection not recognized.")
            sys.exit(1)

    selection = Prompt.ask("[bold cyan][?][/bold cyan] Enter item index, list, or range [dim](or Press Enter for ALL)[/dim]").strip()
    if not selection: selection = 'all'
    player = Prompt.ask("[bold cyan][?][/bold cyan] Select Media Player Engine", choices=["mpv", "vlc"], default="mpv")
    return input_path, db_id, selection, player, False

def main():
    """Standalone CLI entry point for 'bunkr-stream'."""
    import argparse

    from ..core.db import DatabaseManager

    parser = argparse.ArgumentParser(description="bunkr-api Standalone Streamer CLI")
    parser.add_argument('--db-id', type=int, help="Database ID of the album to jump straight into it")
    parser.add_argument('-n', '--number', type=str, help="File selection (e.g. 1,3,5-10)")
    parser.add_argument('--player', choices=['mpv', 'vlc'], default=None, help="Choose a media player")
    parser.add_argument('--staged', action='store_true', help="Stream all staged items")
    args = parser.parse_args()

    db = DatabaseManager()
    engine = PlayerEngine(db)
    
    input_json_path = None
    db_id = args.db_id
    selection_arg = args.number
    player_choice = args.player
    run_staged = args.staged

    if not db_id and not run_staged and len(sys.argv) == 1:
        input_json_path, db_id, selection_arg, player_choice, run_staged = prompt_for_inputs(db)
    
    if not player_choice: player_choice = 'mpv'

    files_list = []

    if run_staged:
        console.print("[bold cyan][*] Extracting all active staged files...[/bold cyan]")
        with db.connection() as conn:
            rows = conn.execute("""
                SELECT a.* FROM assets a
                LEFT JOIN albums al ON a.album_id = al.id
                WHERE a.is_staged = 1 OR al.is_staged = 1
                ORDER BY a.album_id, a.track_number ASC;
            """).fetchall()
            for asset in rows:
                files_list.append({
                    "id": asset["id"],
                    "title": asset["title"] or asset["original_filename"],
                    "signed_cdn_url": asset["signed_cdn_url"],
                    "token_expiry_timestamp": asset["token_expiry_timestamp"],
                    "true_file_id": asset["true_file_id"]
                })
    elif db_id:
        console.print(f"[*] Querying database tracker for Album ID: {db_id}...")
        assets = db.get_album_assets(db_id)
        for asset in assets:
            files_list.append({
                "id": asset["id"],
                "title": asset["title"] or asset["original_filename"],
                "signed_cdn_url": asset["signed_cdn_url"],
                "token_expiry_timestamp": asset["token_expiry_timestamp"],
                "true_file_id": asset["true_file_id"]
            })
    elif input_json_path:
        console.print(f"[*] Reading legacy fallback catalog from {input_json_path}...")
        with open(input_json_path, encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("files_found", []):
            files_list.append({
                "id": None,
                "title": item.get("original") or item.get("title"),
                "signed_cdn_url": item.get("signed_cdn_url"),
                "token_expiry_timestamp": None,
                "true_file_id": item.get("true_file_id")
            })

    if not files_list:
        console.print("[bold red][!] No assets available to stream.[/bold red]")
        return

    try:
        indices = parse_selection(selection_arg or 'all', total_items=len(files_list))
    except ValueError as e:
        console.print(f"[bold red][!] {e}[/bold red]")
        return

    selected_assets = [files_list[i-1] for i in indices]
    db_backed_assets = [a for a in selected_assets if a["id"] is not None]

    if db_backed_assets:
        loop_f = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
        if loop_f:
            asyncio.run(engine.resolve_tokens_async(db_backed_assets), loop_factory=loop_f)
        else:
            asyncio.run(engine.resolve_tokens_async(db_backed_assets))

    queue = []
    for i in indices:
        item = files_list[i-1]
        url = db.get_valid_url(item['id']) if item['id'] else item['signed_cdn_url']
        if url:
            queue.append((i, item['title'], url))

    if player_choice == 'vlc':
        engine.play_vlc(queue)
    else:
        engine.play_mpv(queue)

if __name__ == "__main__":
    main()