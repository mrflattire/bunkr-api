import json
import argparse
import subprocess
import shutil
import sys
import os
import tempfile
import socket
import time
import threading
from pathlib import Path
from rich.prompt import Prompt
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn

console = Console()

def parse_arguments():
    """Parse command line arguments for the streamer script."""
    parser = argparse.ArgumentParser(description="Media Streamer Handoff Module.")
    parser.add_argument(
        '-i', '--input',
        type=str,
        default=None,
        help="Path to the enriched album JSON file."
    )
    parser.add_argument(
        '-n', '--number',
        type=str,
        default=None,
        help=(
            "Restrict streaming to specific item(s) from the JSON. "
            "Accepts a single index ('5'), a comma list ('3,7,12'), a range "
            "('10-20'), or a mix ('1,4-6,9'). Omit or pass 'all' to play everything."
        )
    )
    return parser.parse_args()


def parse_selection(spec: str, total_files: int) -> set:
    """Parses a -n/--number spec like '1,4-6,9' into a set of 1-based indices."""
    if not spec or spec.strip().lower() == 'all':
        return set(range(1, total_files + 1))

    selected = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            try:
                start_str, end_str = chunk.split("-", 1)
                start, end = int(start_str), int(end_str)
                if start > end:
                    start, end = end, start
                selected.update(range(start, end + 1))
            except ValueError:
                raise ValueError(f"Invalid range format in chunk: '{chunk}'")
        else:
            try:
                selected.add(int(chunk))
            except ValueError:
                raise ValueError(f"Invalid numeric index: '{chunk}'")

    out_of_range = {i for i in selected if i < 1 or i > total_files}
    if out_of_range:
        raise ValueError(
            f"Index/indices {sorted(out_of_range)} out of range "
            f"(file has {total_files} items, valid range is 1-{total_files})."
        )
    return selected


def clean_dragged_path(raw: str) -> str:
    """Normalizes a path typed or drag-and-dropped into the terminal."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    text = text.replace("\\ ", " ")
    return text


def connect_to_ipc(ipc_path: str):
    """
    Establishes a read/write file-like handle to the MPV IPC server.
    Handles Unix Sockets on Unix, and Named Pipes natively on Windows.
    """
    if os.name == 'nt':
        # Open Windows named pipe directly as an unbuffered, shared binary file
        try:
            handle = open(ipc_path, 'r+b', buffering=0)
            return handle
        except Exception as e:
            raise ConnectionError(f"Failed to open Windows named pipe {ipc_path}: {e}")
    else:
        # Standard AF_UNIX connection on macOS/Linux
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(ipc_path)
        return sock.makefile("rw", encoding="utf-8")


def poll_mpv_status(ipc_path: str, stop_event: threading.Event, total_tracks: int):
    """
    Subscribes to MPV property changes via JSON IPC and updates the Rich Live UI
    whenever MPV pushes new state data.
    """
    connected = False
    sock_file = None
    
    # 1. Connect to the socket or named pipe
    for _ in range(50):
        if stop_event.is_set():
            return
        if os.name == 'nt' or os.path.exists(ipc_path):
            try:
                sock_file = connect_to_ipc(ipc_path)
                connected = True
                break
            except Exception:
                pass
        time.sleep(0.1)

    if not connected or not sock_file:
        return

    # Local state storage for our observed properties
    state = {
        "media-title": None,
        "time-pos": None,
        "duration": None,
        "percent-pos": None,
        "demuxer-cache-duration": None,
        "playlist-pos": None
    }

    # Map subscription IDs to our properties
    observed_properties = {
        1: "media-title",
        2: "time-pos",
        3: "duration",
        4: "percent-pos",
        5: "demuxer-cache-duration",
        6: "playlist-pos"
    }

    # 2. Subscribe to property changes (Observer Pattern)
    try:
        for obs_id, prop_name in observed_properties.items():
            payload = json.dumps({"command": ["observe_property", obs_id, prop_name]}) + "\n"
            if os.name == 'nt':
                sock_file.write(payload.encode('utf-8'))
            else:
                sock_file.write(payload)
        if os.name != 'nt':
            sock_file.flush()
    except Exception:
        try:
            sock_file.close()
        except Exception:
            pass
        return

    # Setup Rich live layout
    progress_bar = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.fields[time_pos]} / {task.fields[duration]})"),
        TextColumn("[yellow]Cache: {task.fields[cache_duration]}s"),
    )
    
    track_task = progress_bar.add_task(
        description="Buffering...",
        total=100,
        time_pos="00:00",
        duration="00:00",
        cache_duration="0.0"
    )

    def fmt_time(seconds):
        if seconds is None:
            return "00:00"
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    # UI updates will run reflecting the event-driven state dict
    def update_ui():
        name = state["media-title"]
        pos = state["time-pos"]
        dur = state["duration"]
        percent = state["percent-pos"]
        cache = state["demuxer-cache-duration"]
        playlist_pos = state["playlist-pos"]

        if playlist_pos is not None:
            track_num = int(playlist_pos) + 1
            track_prefix = f"[{track_num}/{total_tracks}] "
        else:
            track_prefix = ""

        if name:
            display_title = f"{track_prefix}{name}"
            # Truncate clean window title to prevent layout wrapping issues
            display_title = (display_title[:38] + '...') if len(display_title) > 41 else display_title
        else:
            display_title = "Buffering stream..."

        # Update Live Widget safely
        progress_bar.update(
            track_task,
            description=f"Playing: {display_title}",
            completed=int(percent) if percent is not None else 0,
            time_pos=fmt_time(pos),
            duration=fmt_time(dur),
            cache_duration=f"{cache:.1f}" if cache is not None else "0.0"
        )

    # 3. Read incoming event updates continuously
    with Live(Panel(progress_bar, title="[bold green]Live Stream Player Status[/bold green]", border_style="green"), refresh_per_second=10):
        while not stop_event.is_set():
            try:
                if os.name == 'nt':
                    raw_line = sock_file.readline()
                    if not raw_line:
                        break  # Pipe closed by MPV exiting
                    line = raw_line.decode('utf-8', errors='ignore').strip()
                else:
                    line = sock_file.readline().strip()
                    if not line:
                        break

                data = json.loads(line)
                
                # Check if this is a property-change notification we subscribed to
                if data.get("event") == "property-change":
                    obs_id = data.get("id")
                    prop_name = observed_properties.get(obs_id)
                    if prop_name:
                        state[prop_name] = data.get("data")
                        update_ui()
                        
            except (json.JSONDecodeError, TypeError):
                continue
            except Exception:
                break

    try:
        sock_file.close()
    except Exception:
        pass


def play_playlist_mode(playback_queue: list):
    """Assembles an M3U playlist and opens it in MPV using JSON IPC."""
    console.print(f"\n[bold green][*][/bold green] Assembling Playlist ([cyan]{len(playback_queue)} tracks[/cyan])...")
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u", mode="w", encoding="utf-8") as tmp_playlist:
            tmp_playlist.write("#EXTM3U\n")
            for idx, item in playback_queue:
                title = item.get("original", item.get("title", f"Track_{idx}"))
                url = item.get("signed_cdn_url")
                tmp_playlist.write(f"#EXTINF:-1,{idx}. {title}\n")
                tmp_playlist.write(f"{url}\n")
            playlist_path = Path(tmp_playlist.name)
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Failed to generate temporary play-queue file: {e}")
        return

    # Setup dynamic platform-specific IPC identifier
    if os.name == 'nt':
        ipc_path = rf"\\.\pipe\mpv_ipc_{os.getpid()}"
    else:
        ipc_path = os.path.join(tempfile.gettempdir(), f"mpv_ipc_{os.getpid()}.sock")

    cmd = [
        "mpv",
        "--force-window",
        f"--input-ipc-server={ipc_path}",
        "--idle=once",
        str(playlist_path)
    ]

    # Event flag sync for process control loop
    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_mpv_status,
        args=(ipc_path, stop_event, len(playback_queue)),
        daemon=True
    )

    console.print(f"[bold green][*][/bold green] Launching MPV engine with IPC control...")
    
    try:
        # Launch player cleanly with silent outputs to keep terminal space free of noise
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Start state poller thread
        poll_thread.start()
        
        # Block until the window exits or is manually closed
        proc.wait()
        
    except FileNotFoundError:
        console.print(f"[bold red][-][/bold red] Error: 'mpv' executable not found on system PATH.")
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Playback interrupted by user.")
        if 'proc' in locals():
            proc.terminate()
    finally:
        # Signal loop and clean resources
        stop_event.set()
        poll_thread.join(timeout=1.0)
        
        if playlist_path.exists():
            try:
                os.unlink(playlist_path)
            except OSError:
                pass
                
        if os.name != 'nt' and os.path.exists(ipc_path):
            try:
                os.unlink(ipc_path)
            except OSError:
                pass
                
        console.print("[bold green][+][/bold green] Player session closed safely.")


def main():
    # Scenario A: Script run raw without any CLI arguments at all
    if len(sys.argv) == 1:
        # Scan local workspace for quick targets
        try:
            json_files = [f for f in os.listdir('.') if f.endswith('.json') and os.path.isfile(f)]
            if json_files:
                console.print("\n[bold magenta][*] Discovered payload JSON targets in working directory:[/bold magenta]")
                for f in sorted(json_files):
                    console.print(f"  • [yellow]{f}[/yellow]")
                console.print()
        except Exception:
            pass

        raw = Prompt.ask("[bold cyan][?][/bold cyan] Path to the album JSON file")
        input_json_path = Path(clean_dragged_path(raw)).expanduser()
        
        selection_arg = Prompt.ask("[bold cyan][?][/bold cyan] Enter item index or range [dim](or Press Enter for ALL)[/dim]").strip()
        if not selection_arg:
            selection_arg = 'all'
            
    # Scenario B: Script run with CLI arguments (e.g. -i album.json)
    else:
        args = parse_arguments()
        input_json_path = Path(clean_dragged_path(args.input)).expanduser()
        
        # If the user passed -n / --number, bypass the prompt and use it directly
        if args.number is not None:
            selection_arg = args.number
        # If they omitted -n, ask them interactively, defaulting to 'all' on Enter[cite: 3]
        else:
            selection_arg = Prompt.ask("[bold cyan][?][/bold cyan] Enter item index or range [dim](or Press Enter for ALL)[/dim]").strip()
            if not selection_arg:
                selection_arg = 'all'

    # Load resources
    try:
        with open(input_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Failed to read/parse JSON: {e}")
        sys.exit(1)

    files_list = data.get("files_found", [])
    if not files_list:
        console.print("[bold red][-][/bold red] JSON payload does not contain target files list.")
        sys.exit(1)

    try:
        target_indices = parse_selection(selection_arg, len(files_list))
    except ValueError as exc:
        console.print(f"[bold red][-][/bold red] Error: {exc}")
        sys.exit(1)

    # Populate active play tracks
    playback_queue = []
    for idx, item in enumerate(files_list, start=1):
        if idx in target_indices:
            url = item.get("signed_cdn_url")
            if url:
                playback_queue.append((idx, item))
            else:
                title = item.get("original", item.get("title", f"Track_{idx}"))
                console.print(f"[bold yellow][!][/bold yellow] Skipping Item {idx} - '{title[:30]}' (Requires signature token refresh).")

    if not playback_queue:
        console.print("[bold red][-][/bold red] No playable tracks available. Run minter.py.")
        sys.exit(1)

    # Launch Playback Mode!
    play_playlist_mode(playback_queue)


if __name__ == "__main__":
    # If on Windows, run an empty command with shell=True to initialize Virtual Terminal / ANSI color support
    if os.name == 'nt':
        subprocess.run("", shell=True)
    main()