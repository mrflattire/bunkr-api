import json
import socket
import time
import threading
import subprocess
import tempfile
import shutil
import os
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn

# Internal package imports
from ..core.tokens import mint_single_url_async

console = Console()

class PlayerEngine:
    def __init__(self, db):
        self.db = db

    def connect_to_ipc(self, ipc_path: str):
        """Establishes connection to MPV IPC (Named Pipe on Win, Unix Socket on Linux)."""
        if os.name == 'nt':
            try:
                return open(ipc_path, 'r+b', buffering=0)
            except Exception as e:
                raise ConnectionError(f"Failed to open Windows named pipe {ipc_path}: {e}")
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(ipc_path)
            return sock.makefile("rw", encoding="utf-8")

    def poll_mpv_status(self, ipc_path: str, stop_event: threading.Event, total_tracks: int):
        """Restored: Subscribes to MPV properties and renders the Rich Live UI."""
        sock_file = None
        for _ in range(50):
            if stop_event.is_set(): return
            if os.name == 'nt' or os.path.exists(ipc_path):
                try:
                    sock_file = self.connect_to_ipc(ipc_path)
                    break
                except: pass
            time.sleep(0.1)

        if not sock_file: return

        state = {"media-title": None, "time-pos": None, "duration": None, "percent-pos": None, "cache": None, "playlist-pos": None}
        observed = {1: "media-title", 2: "time-pos", 3: "duration", 4: "percent-pos", 5: "demuxer-cache-duration", 6: "playlist-pos"}

        try:
            for obs_id, prop_name in observed.items():
                payload = json.dumps({"command": ["observe_property", obs_id, prop_name]}) + "\n"
                if os.name == 'nt': sock_file.write(payload.encode('utf-8'))
                else: sock_file.write(payload)
            if os.name != 'nt': sock_file.flush()
        except: return

        progress_bar = Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.fields[time_pos]} / {task.fields[duration]})"),
            TextColumn("[yellow]Cache: {task.fields[cache]}s"),
        )
        track_task = progress_bar.add_task("Buffering...", total=100, time_pos="00:00", duration="00:00", cache="0.0")

        def fmt(s):
            if s is None: return "00:00"
            return f"{int(s)//60:02d}:{int(s)%60:02d}"

        with Live(Panel(progress_bar, title="[bold green]Live Player Status[/bold green]", border_style="green"), refresh_per_second=10):
            while not stop_event.is_set():
                try:
                    line = sock_file.readline()
                    if not line: break
                    data = json.loads(line.decode('utf-8') if isinstance(line, bytes) else line)
                    if data.get("event") == "property-change":
                        prop = observed.get(data.get("id"))
                        if prop: state[prop] = data.get("data")
                        
                        t_prefix = f"[{int(state['playlist-pos'])+1}/{total_tracks}] " if state['playlist-pos'] is not None else ""
                        progress_bar.update(track_task, 
                                            description=f"Playing: {t_prefix}{state['media-title'] or '...'}"[:45],
                                            completed=int(state['percent-pos'] or 0), 
                                            time_pos=fmt(state['time-pos']), 
                                            duration=fmt(state['duration']),
                                            cache=f"{state['cache']:.1f}" if state['cache'] else "0.0")
                except: break
        sock_file.close()

    async def resolve_tokens_async(self, assets):
        """Restored: Parallel token refresh for streaming with NULL safety."""
        from curl_cffi.requests import AsyncSession
        now = time.time()
        
        needed = []
        for a in assets:
            # Handle dictionary or sqlite3.Row objects
            asset_dict = dict(a)
            url = asset_dict.get("signed_cdn_url")
            expiry = asset_dict.get("token_expiry_timestamp")
            
            # If no URL OR expiry is missing OR expiry is within 60 seconds
            if not url or expiry is None or expiry < (now + 60):
                needed.append(asset_dict)
        
        if not needed: return
        
        console.print(f"[*] Batch-refreshing [cyan]{len(needed)}[/cyan] playback tokens...")
        sem = asyncio.Semaphore(4)
        
        async def worker(session, a):
            async with sem:
                try:
                    # Ensure we have a valid ID to mint
                    fid = str(a.get("true_file_id") or a.get("slug_id"))
                    url = await mint_single_url_async(session, fid)
                    self.db.update_asset_url(a["id"], url)
                except Exception as e:
                    console.print(f"[dim red]Minting failed for {a.get('title')[:20]}: {e}[/dim red]")
        
        async with AsyncSession(impersonate="chrome") as session:
            await asyncio.gather(*[worker(session, a) for a in needed])

    def play_mpv(self, playback_queue):
        """Restored: Assembles M3U and launches MPV with IPC polling."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u", mode="w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for idx, title, url in playback_queue:
                f.write(f"#EXTINF:-1,{idx}. {title}\n{url}\n")
            p_path = f.name

        ipc = rf"\\.\pipe\mpv_{os.getpid()}" if os.name == 'nt' else os.path.join(tempfile.gettempdir(), f"mpv_{os.getpid()}")
        stop_event = threading.Event()
        poll_thread = threading.Thread(target=self.poll_mpv_status, args=(ipc, stop_event, len(playback_queue)), daemon=True)

        try:
            proc = subprocess.Popen(["mpv", "--force-window", f"--input-ipc-server={ipc}", "--idle=once", p_path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            poll_thread.start()
            proc.wait()
        finally:
            stop_event.set()
            if os.path.exists(p_path): os.unlink(p_path)

    def play_vlc(self, playback_queue):
        """Restored: VLC handoff logic."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u", mode="w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for idx, title, url in playback_queue: f.write(f"#EXTINF:-1,{idx}. {title}\n{url}\n")
            p_path = f.name
        
        vlc = "vlc"
        if os.name == 'nt' and not shutil.which("vlc"):
            vlc = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
            
        subprocess.run([vlc, p_path])
        if os.path.exists(p_path): os.unlink(p_path)