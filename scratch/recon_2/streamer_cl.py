import json
import argparse
import subprocess
import shutil
import sys
import os
import socket
import threading
import time
import uuid
import tempfile
from pathlib import Path
from rich.prompt import Prompt
from rich.console import Console
from rich.live import Live
from rich.text import Text

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
    parser.add_argument(
        '-p', '--player',
        type=str,
        choices=['mpv', 'vlc'],
        default=None,
        help="Preferred media player to launch (mpv or vlc)."
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


def detect_available_players():
    """Detects which media players are available on the system PATH."""
    available = []
    if shutil.which("mpv"):
        available.append("mpv")
    if shutil.which("vlc"):
        available.append("vlc")
    return available


def prompt_for_inputs():
    """
    Scans the working directory for payload JSONs, prints them out cleanly,
    and prompts the user interactively for target path, items, and player selection.
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
    input_path = None
    while True:
        raw = Prompt.ask("[bold cyan][?][/bold cyan] Path to the album JSON file")
        candidate = Path(clean_dragged_path(raw)).expanduser()
        if candidate.exists() and candidate.is_file():
            input_path = candidate
            break
        console.print(f"[bold red][-][/bold red] Error: '{candidate}' doesn't exist or isn't a file. Try again.")

    # 3. Prompt for targeted index selection
    selection = Prompt.ask(
        "[bold cyan][?][/bold cyan] Enter item index, list, or range [dim](e.g. 5 | 3,7,12 | 1-10 or Press Enter for ALL)[/dim]"
    ).strip()
    if not selection:
        selection = 'all'

    # 4. Detect and prompt for preferred player
    available = detect_available_players()
    player_choice = None

    if not available:
        console.print("[bold red][!][/bold red] Warning: Neither 'mpv' nor 'vlc' was detected on your system PATH.")
        player_choice = Prompt.ask(
            "[bold cyan][?][/bold cyan] Force launch choice anyway?",
            choices=["mpv", "vlc"],
            default="mpv"
        )
    elif len(available) == 1:
        console.print(f"[bold green][*][/bold green] Detected only [cyan]{available[0]}[/cyan] on your system. Using it.")
        player_choice = available[0]
    else:
        player_choice = Prompt.ask(
            "[bold cyan][?][/bold cyan] Select preferred media player",
            choices=["mpv", "vlc"],
            default="mpv"
        )

    return input_path, selection, player_choice


class MpvIpcConnection:
    """
    Thin wrapper so the listener doesn't need to know whether it's talking
    to a POSIX Unix socket or a Windows named pipe -- both platforms end up
    exposing a raw, unbuffered binary .file with write/flush/readline.
    Unbuffered is deliberate: mpv pushes property-change events on its own
    schedule, so buffering would sit on a line until some arbitrary
    threshold was hit instead of surfacing it the moment it arrives.
    """
    def __init__(self, file, sock=None):
        self.file = file
        self._sock = sock  # only set on POSIX; the socket itself needs its own close()

    def close(self):
        try:
            self.file.close()
        except Exception:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass


def build_ipc_path() -> str:
    """
    mpv's --input-ipc-server expects a different kind of path depending on
    platform: a Windows named pipe (\\\\.\\pipe\\name) on Windows, or a
    Unix domain socket file path everywhere else. tempfile.gettempdir() is
    used for the POSIX case since AF_UNIX socket paths have a length limit
    (~104-108 bytes on macOS/BSD) that a playlist's own directory might
    exceed.
    """
    if os.name == "nt":
        return rf"\\.\pipe\mpv_ipc_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    return os.path.join(tempfile.gettempdir(), f"mpv_ipc_{os.getpid()}_{uuid.uuid4().hex[:8]}.sock")


def connect_to_mpv_ipc(ipc_path: str, timeout: float = 5.0):
    """
    Connects to mpv's JSON IPC endpoint, retrying briefly since mpv creates
    it itself shortly after the process starts (not instantly). Returns a
    connected MpvIpcConnection wrapping a raw, unbuffered binary stream, or
    None if it never appeared in time.

    On Windows this opens the named pipe mpv actually creates for
    --input-ipc-server directly as a raw file, deliberately avoiding
    pywin32 -- AF_UNIX sockets are a different OS primitive that mpv
    doesn't use for IPC on Windows, so this still has to branch on
    platform, just without the extra C-binding dependency.
    """
    deadline = time.monotonic() + timeout

    if os.name == "nt":
        while time.monotonic() < deadline:
            try:
                f = open(ipc_path, "r+b", buffering=0)
                return MpvIpcConnection(file=f)
            except OSError:
                # Pipe doesn't exist yet, or another instance is mid-connect
                time.sleep(0.1)
        return None

    sock = None
    while time.monotonic() < deadline:
        if os.path.exists(ipc_path):
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(ipc_path)
                return MpvIpcConnection(file=sock.makefile("rwb", buffering=0), sock=sock)
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                # Socket file exists but mpv isn't listening on it just yet
                if sock:
                    sock.close()
                sock = None
        time.sleep(0.1)
    return None


# IPC request IDs for observe_property subscriptions -> the property each
# one tracks. mpv echoes this id back on every property-change event for
# that property, which is how we route incoming events to local state
# without ever having to correlate a request with its response.
OBSERVED_PROPERTIES = {
    1: "media-title",
    2: "time-pos",
    3: "duration",
    4: "pause",
    5: "percent-pos",
    6: "eof-reached",
}


def send_ipc_command(conn: "MpvIpcConnection", command: dict):
    """Writes one JSON IPC command as a line of UTF-8 bytes."""
    payload = (json.dumps(command) + "\n").encode("utf-8")
    conn.file.write(payload)
    try:
        conn.file.flush()
    except Exception:
        pass  # unbuffered streams don't strictly need this, but it's harmless


def subscribe_to_properties(conn: "MpvIpcConnection"):
    """
    Sends one observe_property command per tracked property. mpv responds
    to each subscription by immediately emitting a property-change event
    with the CURRENT value, so there's no separate "give me the initial
    state" step needed -- the first events double as that.
    """
    for req_id, prop_name in OBSERVED_PROPERTIES.items():
        send_ipc_command(conn, {"command": ["observe_property", req_id, prop_name]})


def format_time(seconds) -> str:
    """Formats a float seconds value as H:MM:SS or M:SS."""
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_mpv_state(state: dict) -> Text:
    """Turns the locally-merged property state into the live status line."""
    title = state.get("media-title") or "..."
    pos = state.get("time-pos")
    dur = state.get("duration")
    paused = state.get("pause")

    status_word = "[yellow]paused[/yellow]" if paused else "[green]playing[/green]"
    pos_str = format_time(pos)
    dur_str = format_time(dur)
    percent = state.get("percent-pos")
    percent_str = f"{percent:5.1f}%" if percent is not None else "  ?  "

    return Text.from_markup(
        f"[cyan]{title}[/cyan]  {status_word}  {pos_str} / {dur_str}  ({percent_str})"
    )


def listen_for_mpv_events(ipc_path: str, stop_event: threading.Event, live: Live):
    """
    Runs in a background thread for the lifetime of mpv playback.

    Subscribes once to a fixed set of properties via observe_property, then
    blocks on readline() waiting for mpv to push property-change events --
    it never sends another request after the initial subscriptions. This
    avoids the request/response desync that a query-based approach hits:
    mpv's IPC channel also carries its own unsolicited event traffic, so a
    "write query, then readline for the answer" pattern can just as easily
    read back someone else's event line instead of the reply it expected.
    Observing sidesteps that -- there's nothing to correlate, just a stream
    of events to fold into local state.

    Exits cleanly (no exception propagation) the moment the pipe/socket
    closes, which happens naturally when mpv itself exits.
    """
    conn = connect_to_mpv_ipc(ipc_path)
    if conn is None:
        live.update(Text("[Playback] Could not attach to mpv for live status.", style="yellow"))
        return

    state = {name: None for name in OBSERVED_PROPERTIES.values()}

    try:
        subscribe_to_properties(conn)

        while not stop_event.is_set():
            raw_line = conn.file.readline()
            if not raw_line:
                # Empty read on a blocking stream means the other end closed --
                # mpv exited (or the pipe/socket broke), either way we're done.
                break

            try:
                message = json.loads(raw_line.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # skip any line we can't parse rather than dying on it

            if message.get("event") != "property-change":
                continue  # ignore mpv's log lines, start-file/end-file, etc.

            prop_name = OBSERVED_PROPERTIES.get(message.get("id"))
            if prop_name is None:
                continue  # a property-change for something we didn't subscribe to

            state[prop_name] = message.get("data")
            live.update(render_mpv_state(state))
    except (BrokenPipeError, ConnectionResetError, OSError):
        # mpv closed the connection -- normal end-of-life, not an error worth surfacing
        pass
    finally:
        conn.close()


def launch_mpv_with_status(playlist_path: Path):
    """
    Launches mpv with an IPC endpoint attached (a Unix socket on POSIX, a
    named pipe on Windows) and polls it on a background thread to render a
    live-updating status line while it plays.
    """
    ipc_path = build_ipc_path()
    stop_event = threading.Event()
    proc = None
    status_thread = None

    try:
        proc = subprocess.Popen([
            "mpv", "--force-window", "--idle=once",
            f"--input-ipc-server={ipc_path}",
            str(playlist_path),
        ])

        with Live(Text("[Playback] Connecting to mpv..."), console=console, refresh_per_second=4) as live:
            status_thread = threading.Thread(
                target=listen_for_mpv_events, args=(ipc_path, stop_event, live), daemon=True
            )
            status_thread.start()

            try:
                proc.wait()
            except KeyboardInterrupt:
                # Stop polling first so the thread isn't reading from a
                # connection we're about to yank the process out from under.
                stop_event.set()
                live.update(Text("[Playback] Interrupted -- closing mpv...", style="yellow"))
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise

    except subprocess.CalledProcessError:
        console.print("[bold yellow][!][/bold yellow] mpv exited with a non-zero status.")
    except FileNotFoundError:
        console.print("[bold red][-][/bold red] Error: 'mpv' not found. Is it installed?")
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Playback interrupted by user.")
    finally:
        stop_event.set()
        if status_thread is not None:
            status_thread.join(timeout=2)
        # POSIX leaves a real socket file behind that mpv doesn't clean up
        # itself; Windows named pipes have no on-disk artifact to remove,
        # so this is a no-op there.
        try:
            if os.name != "nt" and os.path.exists(ipc_path):
                os.remove(ipc_path)
        except OSError:
            pass


def launch_player(player: str, playlist_path: Path):
    """Launches the selected player and feeds it the temporary M3U playlist."""
    console.print(f"\n[bold green][+][/bold green] Streaming via [bold cyan]{player.upper()}[/bold cyan]... Close the player window to return.")
    if player == "mpv":
        launch_mpv_with_status(playlist_path)
        return
    try:
        if player == "vlc":
            subprocess.run(["vlc", str(playlist_path)], check=True)
    except subprocess.CalledProcessError:
        console.print("[bold yellow][!][/bold yellow] Media player process exited with a non-zero status.")
    except FileNotFoundError:
        console.print(f"[bold red][-][/bold red] Error: Core executable '{player}' not found. Is it installed?")
    except KeyboardInterrupt:
        console.print("\n[bold yellow][!][/bold yellow] Playback interrupted by user.")


def main():
    # 1. Gather inputs via CLI or Interactive fallbacks
    if len(sys.argv) == 1:
        input_json_path, selection_arg, player = prompt_for_inputs()
    else:
        args = parse_arguments()
        
        # Resolve Input JSON
        if args.input:
            input_json_path = Path(clean_dragged_path(args.input)).expanduser()
            if not input_json_path.exists():
                console.print(f"[bold red][-][/bold red] Error: '{input_json_path}' not found.")
                sys.exit(1)
        else:
            # If some args are passed but no input, prompt just for input
            input_json_path, _, _ = prompt_for_inputs()
            
        selection_arg = args.number if args.number else "all"
        
        # Player resolution
        if args.player:
            player = args.player
        else:
            available = detect_available_players()
            player = available[0] if available else "mpv"

    # 2. Parse and validate JSON data
    try:
        with open(input_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Failed to read/parse JSON database: {e}")
        sys.exit(1)

    files_list = data.get("files_found", [])
    if not files_list:
        console.print("[bold red][-][/bold red] JSON payload structure does not contain 'files_found'.")
        sys.exit(1)

    total_files = len(files_list)

    # 3. Resolve selection
    try:
        target_indices = parse_selection(selection_arg, total_files)
    except ValueError as exc:
        console.print(f"[bold red][-][/bold red] Error: {exc}")
        sys.exit(1)

    # Filter our playlist items based on selected 1-based indices
    playback_queue = []
    for idx, item in enumerate(files_list, start=1):
        if idx in target_indices:
            url = item.get("signed_cdn_url")
            if url:
                playback_queue.append((idx, item))
            else:
                console.print(f"[bold yellow][!][/bold yellow] Skipping Item {idx} - Title: [dim]'{item.get('title')[:30]}'[/dim] (No active token signature signed). Run minter first!")

    if not playback_queue:
        console.print("[bold red][-][/bold red] No playable files found in your selection. Make sure to run the signature minter first!")
        sys.exit(1)

    # 4. Generate M3U playlist file on-the-fly
    console.print(f"\n[bold green][*][/bold green] Assembling Playlist ([cyan]{len(playback_queue)} tracks[/cyan])...")
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u", mode="w", encoding="utf-8") as tmp_playlist:
            tmp_playlist.write("#EXTM3U\n")
            for idx, item in playback_queue:
                title = item.get("title", f"Track_{idx}")
                url = item.get("signed_cdn_url")
                # Write metadata and signed stream target
                tmp_playlist.write(f"#EXTINF:-1,{idx}. {title}\n")
                tmp_playlist.write(f"{url}\n")
            playlist_path = Path(tmp_playlist.name)
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Failed to generate temporary play-queue file: {e}")
        sys.exit(1)

    # 5. Launch the Media Player
    try:
        launch_player(player, playlist_path)
    finally:
        # Guarantee cleanup of the temporary file after player exits
        if playlist_path.exists():
            try:
                os.unlink(playlist_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()