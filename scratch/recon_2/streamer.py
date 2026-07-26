import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

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


def launch_player(player: str, playlist_path: Path):
    """Launches the selected player and feeds it the temporary M3U playlist."""
    console.print(f"\n[bold green][+][/bold green] Streaming via [bold cyan]{player.upper()}[/bold cyan]... Close the player window to return.")
    try:
        if player == "mpv":
            # --force-window ensures mpv starts an interface even if playing purely audio files
            subprocess.run(["mpv", "--force-window", str(playlist_path)], check=True)
        elif player == "vlc":
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