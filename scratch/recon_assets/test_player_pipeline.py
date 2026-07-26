# test_player_pipeline.py
import json
import subprocess
import time

from rich.console import Console

console = Console()

# Define the Windows Named Pipe path for MPV IPC
IPC_PIPE_PATH = r"\\.\pipe\mpv-ipc-test"

def get_test_asset():
    """Queries the database for the first asset with a valid, active CDN URL."""
    from core import DatabaseManager
    db = DatabaseManager()
    
    # Grab assets
    with db._get_connection() as conn:
        row = conn.execute("""
            SELECT id, title, signed_cdn_url, token_expiry_timestamp 
            FROM assets 
            WHERE signed_cdn_url IS NOT NULL 
              AND token_expiry_timestamp > ? 
            LIMIT 1;
        """, (int(time.time()),)).fetchone()
        
    return dict(row) if row else None

def send_ipc_command(pipe_path: str, command: dict) -> dict:
    """Sends a JSON command over a Windows Named Pipe and reads the response."""
    # Windows named pipes are opened like files
    try:
        with open(pipe_path, "r+b", buffering=0) as pipe:
            # Format command as a newline-terminated JSON string
            payload = json.dumps(command).encode("utf-8") + b"\n"
            pipe.write(payload)
            
            # Read response
            response = pipe.readline().decode("utf-8").strip()
            return json.loads(response)
    except Exception as e:
        return {"error": str(e)}

def run_pipeline_test():
    console.print("[bold yellow][*] Stage 1: Retrieving active CDN asset...[/bold yellow]")
    asset = get_test_asset()
    
    if not asset:
        console.print("[bold red][x] Error:[/bold red] No assets with active, unexpired CDN URLs found in the database.")
        console.print("[dim]Please register an album and run 'mint.py' first to populate valid signatures.[/dim]")
        return
        
    console.print(f"[green][+][/green] Found Asset #[cyan]{asset['id']}[/cyan]: '{asset['title']}'")
    console.print(f"[dim]URL: {asset['signed_cdn_url'][:80]}...[/dim]\n")

    console.print("[bold yellow][*] Stage 2: Spawning MPV with IPC Pipe...[/bold yellow]")
    
    # We pass the --input-ipc-server flag to establish the named pipe
    mpv_cmd = [
        "mpv",
        f"--input-ipc-server={IPC_PIPE_PATH}",
        "--no-video",  # Keep it headless/audio-only for this automated pipeline test
        asset["signed_cdn_url"]
    ]
    
    try:
        # Start MPV in the background
        process = subprocess.Popen(mpv_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        console.print(f"[green][+][/green] MPV process spawned (PID: {process.pid})")
        console.print("[dim]Waiting 2 seconds for IPC socket initialization...[/dim]")
        time.sleep(2)
        
        console.print("\n[bold yellow][*] Stage 3: Attempting JSON-IPC handshake...[/bold yellow]")
        
        # Ping command to ask MPV for its property 'pause' status
        ping_cmd = {
            "command": ["get_property", "pause"]
        }
        
        response = send_ipc_command(IPC_PIPE_PATH, ping_cmd)
        
        if "error" in response and response["error"] != "success":
            console.print(f"[bold red][x] IPC Handshake Failed:[/bold red] {response['error']}")
        else:
            console.print("[bold green][+] IPC Handshake SUCCESSFUL![/bold green]")
            console.print(f"[white]MPV Response Payload:[/white] {response}")
            
            # Send a quick command to pause/resume to prove dynamic control
            console.print("[bold yellow][*] Stage 4: Testing remote command execution (Toggle Pause)...[/bold yellow]")
            toggle_cmd = {"command": ["cycle", "pause"]}
            toggle_res = send_ipc_command(IPC_PIPE_PATH, toggle_cmd)
            console.print(f"[green][+][/green] Toggle command response: {toggle_res}")

    except FileNotFoundError:
        console.print("[bold red][x] Error:[/bold red] 'mpv' executable not found in your system PATH.")
    except Exception as e:
        console.print(f"[bold red][x] Pipeline exception encountered:[/bold red] {e}")
    finally:
        # Cleanup process and pipe
        if 'process' in locals():
            console.print("\n[bold yellow][*] Cleaning up MPV process...[/bold yellow]")
            process.terminate()
            process.wait()
            console.print("[green][+][/green] Process terminated cleanly.")

if __name__ == "__main__":
    run_pipeline_test()