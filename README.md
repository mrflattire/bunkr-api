<div align="center">

# bunkr-api

**Modular Python toolkit and API wrapper for Bunkr**  
Search, catalog, download, and stream media with a high-performance interactive terminal UI.

[![CI](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/mrflattire/bunkr-api/main/ci-badge.json&cacheSeconds=300&&logo=github)](https://github.com/mrflattire/bunkr-api/actions)
[![PyPI version](https://badge.fury.io/py/bunkr-api.svg?icon=si%3Apython&maxAge=300)](https://pypi.org/project/bunkr-api)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/mrflattire/bunkr-api/main/coverage-badge.json&cacheSeconds=300&logo=pytest)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[Features](#-features) • [Installation](#-installation) • [Quick Start](#-quick-start) • [Usage](#-detailed-usage--advanced-workflows)

</div>

## 🚀 Version Status

| Component | Status | Notes |
| :--- | :--- | :--- |
| **Scraper** | ✅ Working | TLS Impersonation (Chrome) for deep metadata resolution. |
| **Downloader** | ✅ Working | Multi-threaded segmented retrieval via `yt-dlp`. |
| **Streamer** | ✅ Working | Real-time IPC syncing with `mpv`. |
| **Database** | ✅ Working | Persistent SQLite tracking in `~/.bunkr_api/`. |

## ✨ Features

* **Interactive Dashboard**: A full-featured terminal hub to manage your catalog, staged files, and downloads.
* **Deep Resolution**: Automatically extracts raw asset IDs from `window.albumFiles` JavaScript arrays.
* **High-Speed Downloads**: Parallel multi-worker queue powered by the industry-standard `yt-dlp` engine.
* **Direct Streaming**: Play content instantly via `mpv` or `vlc` with temporary playlist (M3U) generation.
* **Token Maintenance**: Automated "Escape Hatch" logic to renew expiring signed CDN signatures.
* **Developer Friendly**: Clean Python API (`BunkrAPI`) for integration into third-party scripts.

## 📦 Installation

### CLI (Recommended)

```sh
# Using uv (fastest)
uv tool install bunkr-api

# Using pip
pip install bunkr-api
```

> **Windows users:** `uv tool install` places executables in `%USERPROFILE%\.local\bin`, which is **not** on `PATH` by default on Windows (unlike most Linux/macOS setups). If `bunkr-api`, `bunkr-download`, etc. aren't recognized after install, run:
> ```powershell
> uv tool update-shell
> ```
> then restart your terminal. Alternatively, for just the current session: `$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"`.

### From Source (Editable)

```sh
git clone https://github.com/mrflattire/bunkr-api.git
cd bunkr-api
pip install -e .
```

### Media Players (Required for Streaming)

To stream content directly, ensure one of the following is installed on your system:

* **[MPV](https://mpv.io/)** (Recommended for status syncing)
* **[VLC](https://www.videolan.org/vlc/)**

<details>
<summary>Linux</summary>

```sh
# Ubuntu/Debian
sudo apt install mpv

# Fedora/RHEL
sudo dnf install mpv

# Arch Linux
sudo pacman -S mpv
```
</details>

<details>
<summary>macOS</summary>

```sh
brew install mpv
```
</details>

<details>
<summary>Windows</summary>

Download from [mpv.io/installation](https://mpv.io/installation/).
</details>

---

## 🛠 Prerequisites

Unlike standard video tools, **FFmpeg is not a prerequisite** for this package. The core requirements are:
1. **Python 3.12 or higher**.
2. **yt-dlp** (installed automatically via dependencies).
3. **mpv** or **vlc** (if you intend to use the streaming features).

---

## 🚦 Quick Start

### Command Line Interface

```sh
# Launch the main interactive catalog menu
bunkr-api

# Search for a creator and catalog their albums
bunkr-scrape "creator_name"

# Download a specific album by its database ID
bunkr-download --db-id 5 --workers 5

# Stream a specific album directly to MPV
bunkr-stream --db-id 5
```

### Python API

```python
import asyncio
from bunkr_api import BunkrAPI

async def main():
    api = BunkrAPI()
    
    # Programmatic search and resolution
    results = await api.search("Natalie Roush")
    if results:
        album_id = await api.resolve_album(results[0]['url'])
        print(f"Album registered with ID: {album_id}")
        
        # Trigger parallel download
        await api.download_album(album_id, workers=3)

asyncio.run(main())
```

---

## 📖 Detailed Usage & Advanced Workflows

<details open>
<summary><h3>Main Dashboard Interface (`bunkr-api`)</h3></summary>

The primary entry point of the toolkit. It serves as an interactive hub for managing your entire media catalog. It is designed to intelligently handle inputs—whether you want to browse your collection, jump to a specific album, or import json metadata files.

```sh
bunkr-api [OPTIONS] [PATH/ID]
```

**Common Examples:**

```sh
# Launch the main interactive catalog menu
bunkr-api

# Jump directly to a specific album detail view by its Database ID
bunkr-api --db-id 17
# OR simply (legacy support)
bunkr-api 17

# Import a local JSON metadata file and view it immediately
bunkr-api -i ./album_metadata.json
# OR simply drop the file onto the terminal
bunkr-api "./path/to/metadata.json"

# Perform a quick search and resolution directly from the launch command
bunkr-api "Natalie Roush" --mode broad --per 40
```

**Main Catalog Commands:**

Inside the main menu, use these single-character commands:

| Key | Action |
| :--- | :--- |
| `s` | **Search / Scrape**: Enter a creator name to find and catalog new albums. |
| `t` | **Trending**: Browse the most popular videos, images, or files from the last 24h/7d. |
| `d` | **Delete**: Remove albums from your database (supports single IDs, lists, or ranges). |
| `q` | **Quit**: Safely exit the application. |

**The [交互 Engine] Detail View:**

Once inside an album, the "Action Context" menu allows for granular media control:

1.  **Stream target(s)**: Launch your media player (MPV/VLC). Supports range selection.
2.  **Download target(s)**: Queue specific files for multi-threaded download.
3.  **Download ALL**: Immediately begin retrieving every file in the album.
4.  **Copy link**: Output the specific signed CDN URL for the chosen track to the terminal.
5.  **Mint new tokens**: Triggers a manual batch refresh of CDN signatures with a real-time progress bar.
6.  **Stage/Unstage**: Flag specific items (or the whole album) for later batch retrieval.
7.  **Navigation (`n`/`p`)**: Flip through pages of large asset inventories.

> **Intelligent Routing**: `bunkr-api` prioritizes its arguments. If it detects a path ending in `.json`, it imports it. If it detects a number, it jumps to that ID. Otherwise, it treats the input as a search term to launch the scraper.

### Why this command is the best choice for users:
1.  **Centralized Control**: You never need to leave this interface to perform 99% of your tasks.
2.  **State Management**: It automatically saves your progress; if you "Stage" items, they stay staged until you decide to download them.
3.  **Visual Feedback**: Uses the full power of the `Rich` library to provide a clean, color-coded view of your library's health and token status.

</details>

---

<details>
<summary><h3>Standalone Downloader (`bunkr-download`)</h3></summary>

A high-performance retrieval engine that utilizes a multi-threaded worker pool and the `yt-dlp` backend to maximize your bandwidth. It handles token resolution and directory organization automatically.

```sh
bunkr-download [OPTIONS]
```

**Common Examples:**

```sh
# Download an entire album using 5 concurrent workers
bunkr-download --db-id 17 --workers 5

# Download a specific range of files (e.g., first five files)
bunkr-download --db-id 17 --number 1-5

# The Triage Workflow: Retry all files currently marked as 'FAILED' in the DB
bunkr-download --triage --workers 3

# The Staged Workflow: Download everything globally marked as 'STAGED'
bunkr-download --staged

# Point a download to a specific external hard drive path
bunkr-download --db-id 17 -o "D:/Media/Archive"
```

**Available Options:**

| Option | Description |
| :--- | :--- |
| `--db-id` | The unique ID of the album in your SQLite database. |
| `-w, --workers` | Number of concurrent download threads (Default: 1, Max: 5). |
| `-n, --number` | Restrict download to specific indices (supports `1,2`, `1-5`, or `all`). |
| `-o, --output` | Override the default download directory for this specific run. |
| `--staged` | Process all items globally flagged as staged across the entire database. |
| `--triage` | Automatically aggregate and retry all items with a `FAILED` download status. |
| `-i, --input` | Path to a local legacy JSON file to download without using the database. |

> **Interactive Mode**: If you run `bunkr-download` without any arguments, it launches a catalog browser identical to the main dashboard, allowing you to pick an album and specify workers visually.

### Why this standalone command is useful:
1.  **Error Recovery**: The `--triage` flag is the fastest way to clean up a catalog that had network interruptions during a large batch.
2.  **Automation**: It is perfect for headless environments or cron jobs (e.g., `bunkr-download --staged` running every night).
3.  **Resource Management**: You can precisely control system load by adjusting the `--workers` flag based on your current internet speed and CPU availability.

</details>

---

<details>
<summary><h3>Standalone Streamer (`bunkr-stream`)</h3></summary>

Stream content directly to your media player without downloading. This command automatically triggers the "Escape Hatch" to refresh any expired tokens before playback starts.

```sh
bunkr-stream --db-id ID [OPTIONS]
```

**Common Examples:**

```sh
# Stream an entire album by its Database ID
bunkr-stream --db-id 17

# Stream a specific selection (e.g., tracks 1, 5, and 10 through 15)
bunkr-stream --db-id 17 --number 1,5,10-15

# Force playback in VLC instead of the default MPV
bunkr-stream --db-id 17 --player vlc

# Stream everything currently marked as 'STAGED' in your database
bunkr-stream --staged
```

**Available Options:**

| Option | Description |
| :--- | :--- |
| `--db-id` | The unique ID of the album in your SQLite database. |
| `-n, --number` | Restrict playback to specific indices (supports `1,2`, `1-5`, or `all`). |
| `--player` | Choose your backend engine: `mpv` (default) or `vlc`. |
| `--staged` | Bypass album IDs and stream all items globally flagged as staged. |

> **Note on MPV**: When using MPV, `bunkr-api` opens a JSON IPC server. This allows the terminal to show you exactly which track is playing, the current timestamp, and the buffer/cache duration in real-time.

### Why this standalone command is useful:
1. **Low Latency**: It bypasses the Main Menu logic, allowing you to start video playback in one command.
2. **Headless Maintenance**: If you know an album ID, you can start a stream without ever seeing the catalog list.
3. **Staging Workflow**: You can spend time "Staging" items via `bunkr-api` or `bunkr-inspect`, and then later just run `bunkr-stream --staged` to play your curated queue.

</details>

---

<details>
<summary><h3>Standalone Scraper (`bunkr-scrape`)</h3></summary>

A deep-resolution engine designed to discover albums, extract asset metadata, and synchronize findings with your local database. Bypasses detection and provides real-time feedback during the resolution process.

```sh
bunkr-scrape [QUERY] [OPTIONS]
```

**Common Examples:**

```sh
# Search for a creator and enter the interactive selection menu
bunkr-scrape "InpossibleOreo"

# Perform a strict search with high result volume and custom sorting
bunkr-scrape "Natalie" --mode strict --per 60 --sort mostfiles

# Browse trending videos from the last 24 hours
bunkr-scrape --top videos

# Archive metadata to a specific folder without using the default export path
bunkr-scrape "CreatorName" --save-json --output "C:/Backups/Bunkr_Metadata"
```

**Available Options:**

| Option | Description |
| :--- | :--- |
| `search` | The positional query string (e.g., creator name or album title). |
| `-m, --mode` | Filtering execution mode: `broad`, `strict`, `fuzzy`, `substring`, `whole`. |
| `-p, --per` | Total results requested per page: `20`, `40`, `60`, `100`. |
| `-s, --sort` | Result array sorting metric: `latest`, `oldest`, `mostfiles`. |
| `-t, --top` | Crawl trending layout categories: `albums`, `videos`, `files`, `images`. |
| `--save-json` | Export the resolved album metadata to a `.json` file. |
| `-o, --output` | Override the default directory for JSON exports. |

> **Deep Resolution Intelligence**: Unlike basic scrapers, `bunkr-scrape` performs a deep resolution. It navigates into your selected album, parses the internal JavaScript state, and extracts the **True IDs** required for streaming and downloading.

### Why this standalone command is useful:
1.  **Direct Cataloging**: It is the primary way to "feed" your database. Any album resolved here becomes immediately available in `bunkr-api`, `bunkr-download`, and `bunkr-stream`.
2.  **Metadata Archival**: By using `--save-json`, you can create a portable snapshot of an album's contents (including original filenames and sizes) before a link potentially goes dead.
3.  **Bypass Navigation**: If you know exactly what you are looking for, flags like `--mode strict` and `--sort mostfiles` get you to the correct result much faster than manual browsing.

</details>

---

<details>
<summary><h3>System Management (`bunkr-inspect`)</h3></summary>

The "Swiss Army Knife" for your local database. `bunkr-inspect` allows you to generate reports, manage staging queues, and perform database maintenance or schema migrations without writing raw SQL.

```sh
bunkr-inspect [COMMAND] [TARGET] [OPTIONS]
```

**Common Examples:**

```sh
# View a global summary of your database health and pipeline metrics
bunkr-inspect view dashboard

# Get a detailed per-album breakdown of completion % and total sizes
bunkr-inspect view albums

# Dump every internal field for a specific album (useful for debugging IDs)
bunkr-inspect view album --id 11

# Stage a range of assets globally for the next download run
bunkr-inspect stage asset 50-100

# Repair metadata drift: Recompute file counts and aggregate sizes from actual records
bunkr-inspect db --recount

# Permanently delete specific albums and their associated file records
bunkr-inspect db --wipe 12,15-20
```

**Available Sub-Commands:**

#### 1. `view` (Reporting)
| Target | Description |
| :--- | :--- |
| `dashboard` | The default view. Shows row counts and success/fail metrics for all tables. |
| `albums` | Summarizes every album: completeness, failed counts, and formatted size. |
| `expiring` | Identifies assets with missing or near-expired security tokens. |
| `staged` | Groups all currently staged items by their parent album. |
| `album` | Requires `--id`. A deep, untruncated dump of an album and its files. |
| `[table_name]` | Performs a raw dump of any table (e.g., `assets` or `system_config`). |

#### 2. `stage` (Queue Management)
Used to flag items for batch actions in `bunkr-download` or `bunkr-stream`.
*   **Targets**: `album` (cascades to all files) or `asset` (individual files).
*   **Selection**: Supports IDs, comma-separated lists, ranges, or `all`.
*   **Options**: Use `--off` to remove the staging flag.

#### 3. `db` (Maintenance & Write Ops)
| Option | Description |
| :--- | :--- |
| `--wipe [IDS]` | Deletes albums. Use `--wipe all` to clear everything except settings. |
| `--recount` | Fixes UI drift by recalculating `file_count` from the actual assets table. |
| `--exec "SQL"` | Runs a raw write-statement (UPDATE/DELETE/VACUUM) with confirmation. |
| `--nuke` | Factory reset. Drops all tables and resets config to defaults. |
| `-y, --yes` | Skips confirmation prompts for destructive operations. |

> **Raw Table Dumps**: You can filter any raw table view using `--search "text"` or change the output volume with `--limit 50` or `--all`.

### Why this standalone command is useful:
1.  **Data Integrity**: If the scraper crashes mid-way, `--recount` ensures your dashboard doesn't show "33 files" when only 10 were successfully cataloged.
2.  **Audit Logs**: `view expiring` helps you predict which downloads might fail before you even start them.
3.  **Schema Evolution**: As the project grows, `add-column` allows you to update your local database structure without losing your existing history.

</details>

---

<details>
<summary><h3>Token Maintenance (`bunkr-mint`)</h3></summary>

A dedicated maintenance utility designed to keep your media library "hot." Because Bunkr CDN links use 2-hour time-limited expiration (`ex`) timestamps, they eventually stop working. `bunkr-mint` automates the process of fetching fresh signatures using the stored **True IDs** in your database.

```sh
bunkr-mint [OPTIONS]
```

**Common Examples:**

```sh
# Launch the background daemon
# This will poll your entire database every 60s and refresh tokens near expiry
bunkr-mint

# Perform a one-shot targeted refresh for a specific album and then exit
bunkr-mint --album-id 17
```

**Available Options:**

| Option | Description |
| :--- | :--- |
| `-a, --album-id` | Target a specific album for a single maintenance pass. |
| `-h, --help` | Show the help message and exit. |

**The "Escape Hatch" Workflow:**

While the main `bunkr-api` dashboard triggers an automated "Escape Hatch" refresh when you start a stream or download, running `bunkr-mint` in a separate terminal window is the recommended workflow for heavy users.

*   **Concurrency**: Uses a semaphore-controlled worker pool to refresh dozens of tokens simultaneously without hitting rate limits.
*   **Progress Tracking**: Features a real-time `Rich` progress bar and spinner so you can see exactly which assets are being updated.
*   **Database Sync**: Automatically updates the `signed_cdn_url` and `token_expiry_timestamp` columns in your SQLite database.

> **Pro Tip**: If you are planning a massive download session (100+ files), run `bunkr-mint` in the background first to ensure every link is valid before the download engine starts.

### Why this standalone command is useful:
1.  **Continuous Uptime**: Keeps your catalog ready for instant streaming at any time, even weeks after the initial scrape.
2.  **Headless Operations**: Can be run on a server or a separate monitor to act as a "watchdog" for your media library.
3.  **Low Overhead**: The daemon stays idle most of the time, only consuming network resources when tokens are actually nearing their expiration window (default 10 minutes).

</details>

---

<details>
<summary><h3>Python API (`BunkrAPI`)</h3></summary>

Everything the CLI tools do is also available as a plain Python library, so you can script your own workflows — cron jobs, batch imports, custom dashboards — without touching the terminal UI at all. `BunkrAPI` is a single facade class wrapping the scraper, downloader, player, and database.

```python
from bunkr_api import BunkrAPI

api = BunkrAPI()  # uses the same ~/.bunkr_api/media_tracker.db as the CLI tools
```

> **Async by design**: nearly every method that touches the network, disk, or a subprocess is `async def` including `download_album`, which internally spawns a real worker pool via `loop.run_in_executor()` rather than blocking. This means you can safely `await` these from inside your own event loop (e.g. a web server request handler) without deadlocking. The only fully synchronous methods are the pure catalog lookups (`get_albums`, `get_album`, `get_assets`, `get_valid_url`) and `delete_album`. If you're writing a simple standalone script (not already inside an event loop), wrap your entry point in `asyncio.run(...)` as shown in every example below.

**Full Method Reference:**

#### Catalog & Retrieval *(sync)*
| Method | Description |
| :--- | :--- |
| `get_albums()` | Returns every cataloged album as a list of dicts. |
| `get_album(album_id)` | Returns a single album's metadata, or `None` if it doesn't exist. |
| `get_assets(album_id)` | Returns every asset belonging to an album. |
| `get_valid_url(asset_id)` | Returns a fresh, valid signed CDN URL for one asset, re-minting automatically if the cached token is missing or expiring soon. |

#### Scraping & Resolution *(async)*
| Method | Description |
| :--- | :--- |
| `search(term, mode="broad", per=20, sort="latest")` | Programmatic search. Returns a list of album dicts (`title`, `url`, `file_count`). |
| `resolve_album(url, search_context="API_User", save_json=False)` | Scrapes and registers a bunkr album URL. Returns the new database ID. |
| `resolve_and_download(url, workers=3, output_dir=..., save_json=False)` | One-shot convenience: `resolve_album()` immediately followed by `download_album()`. Returns the database ID. |

#### Media Execution *(async)*
| Method | Description |
| :--- | :--- |
| `download_album(album_id, workers=3, output_dir=...)` | Multi-threaded download for a whole album. Raises `ValueError` if the album doesn't exist. |
| `download_staged(workers=3, output_dir=...)` | Downloads everything currently flagged as staged (see `bunkr-inspect stage` or the dashboard's `6` action). A no-op if nothing is staged. |
| `retry_failed(workers=3, output_dir=...)` | Retries every asset currently marked `FAILED`. A no-op if nothing has failed. |
| `stream_album(album_id, indices_spec="all", player="mpv")` | Resolves tokens and launches `mpv`/`vlc` for the given selection. Raises `ValueError` if the album has no assets. |

#### Maintenance
| Method | Description |
| :--- | :--- |
| `refresh_tokens(album_id=None)` *(async)* | Refreshes any expiring CDN tokens — scoped to one album, or the whole database if omitted. Always a single pass; never blocks forever. |
| `delete_album(album_id)` *(sync)* | Deletes an album and all its assets (cascades at the database level). Returns `True` if something was actually deleted. |

---

**Example — the classic three-step flow:**

```python
import asyncio
from bunkr_api import BunkrAPI

async def main():
    api = BunkrAPI()

    results = await api.search("Natalie Roush")
    if results:
        album_id = await api.resolve_album(results[0]["url"])
        print(f"Album registered with ID: {album_id}")
        await api.download_album(album_id, workers=3)

asyncio.run(main())
```

**Example — the same flow, collapsed into one call:**

```python
import asyncio
from bunkr_api import BunkrAPI

async def main():
    api = BunkrAPI()
    results = await api.search("Natalie Roush")
    if results:
        album_id = await api.resolve_and_download(results[0]["url"], workers=5)
        print(f"Resolved and downloaded album {album_id}")

asyncio.run(main())
```

**Example — browsing your existing catalog without touching the network:**

```python
from bunkr_api import BunkrAPI

api = BunkrAPI()

for album in api.get_albums():
    print(f"#{album['id']} — {album['title']} ({album['file_count']} files)")

# Drill into one album
album = api.get_album(5)
if album:
    for asset in api.get_assets(5):
        print(f"  {asset['title']} — {asset['download_status']}")
        # Get a guaranteed-fresh link, even if the cached token expired
        print(f"  -> {api.get_valid_url(asset['id'])}")
```

**Example — streaming programmatically:**

```python
import asyncio
from bunkr_api import BunkrAPI

async def main():
    api = BunkrAPI()
    # Stream tracks 1, 3, and 5-8 from album 17 in VLC instead of the MPV default
    await api.stream_album(17, indices_spec="1,3,5-8", player="vlc")

asyncio.run(main())
```

**Example — maintenance batch jobs (great for a nightly cron script):**

```python
import asyncio
from bunkr_api import BunkrAPI

async def nightly_maintenance():
    api = BunkrAPI()

    # Keep every token in the database fresh
    await api.refresh_tokens()

    # Pick up anything you staged earlier via `bunkr-inspect stage` or the dashboard
    await api.download_staged(workers=4)

    # Give yesterday's network blips another chance
    await api.retry_failed(workers=4)

asyncio.run(nightly_maintenance())
```

**Example — resolving several albums concurrently with `asyncio.gather`:**

```python
import asyncio
from bunkr_api import BunkrAPI

async def main():
    api = BunkrAPI()
    urls = [
        "https://bunkr.cr/a/first-album",
        "https://bunkr.cr/a/second-album",
        "https://bunkr.cr/a/third-album",
    ]

    # resolve_album() itself is I/O-bound (network), so gathering several
    # at once is a genuine speedup, not just cosmetic concurrency.
    album_ids = await asyncio.gather(*(api.resolve_album(u) for u in urls))
    print(f"Registered {len(album_ids)} albums: {album_ids}")

    # Then download them all, one at a time (download_album already
    # parallelizes its own worker pool internally via `workers=`)
    for album_id in album_ids:
        await api.download_album(album_id, workers=5)

asyncio.run(main())
```

**Example — cleaning up an album you no longer want:**

```python
from bunkr_api import BunkrAPI

api = BunkrAPI()
was_deleted = api.delete_album(5)
print(f"Album 5 removed: {was_deleted}")  # False if it never existed
```

### Why the Python API is useful:
1.  **No CLI required for automation**: cron jobs, scheduled maintenance, or a custom Discord bot can all drive the same database the interactive tools use, with zero terminal interaction.
2.  **Composable**: every method returns plain dicts/lists/ints/bools — no custom objects to learn — so it's trivial to slot into an existing script, web framework, or notebook.
3.  **Safe to embed**: because the network/disk/subprocess methods are genuinely async (not just decorated), you can call them from inside your own already-running event loop — a FastAPI route handler, a Discord.py command, a Jupyter cell — without the classic "asyncio.run() cannot be called from a running event loop" crash.

</details>

---

## 📂 Configuration & Data

The application stores all data in a hidden directory in your user profile to ensure persistence:

*   **SQLite Database**: `~/.bunkr_api/media_tracker.db`
*   **Logs**: `~/.bunkr_api/logs/`
*   **Default Downloads**: `~/Downloads/bunkr_downloads/`
*   **Default JSON Exports**: `~/Downloads/bunkr_downloads/jsons/` — used by both `bunkr-api --save-json` and `bunkr-scrape --save-json` when `-o/--output` isn't given.

> **Pro Tip**: Every command that writes files (`bunkr-scrape --save-json`, `bunkr-download`, etc.) accepts an `-o/--output` flag to override its destination for that run.

---

## ⚖️ Disclaimer

**bunkr-api** is an independent project and is not affiliated with, authorized, maintained, or endorsed by Bunkr. This tool is provided for educational and personal archival purposes only. Users are responsible for complying with the Terms of Service of the platforms they interact with and the copyrights of the content creators.

<div align="center">Made with ❤️ for the terminal natives.</div>
