import argparse
import urllib.parse
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt

console = Console()

# =========================================================================
# CONFIGURATION & MAPPING DICTIONARIES
# =========================================================================
# Standardizing user inputs/flags to exact remote backend URL syntax
SEARCH_MODES = {
    "broad": "broad",
    "strict": "strict",
    "fuzzy": "fuzzy",
    "substring": "substring",
    "whole word": "whole",
    "whole": "whole"
}

SORT_TYPES = {
    "latest": "latest",
    "oldest": "oldest",
    "most files": "mostfiles",
    "mostfiles": "mostfiles"
}

VALID_COUNTS = [20, 40, 60, 100]

# =========================================================================
# CLI ARGUMENT PARSER SETUP
# =========================================================================
parser = argparse.ArgumentParser(description="Bunkr / Balbums Scraper Optimization")
parser.add_argument("search", nargs="?", help="The targeted search term query string")
parser.add_argument("-m", "--mode", choices=["broad", "strict", "fuzzy", "substring", "whole"], help="Filter execution mode")
parser.add_argument("-p", "--per", type=int, choices=VALID_COUNTS, help="Total results requested per engine execution")
parser.add_argument("-s", "--sort", choices=["latest", "oldest", "mostfiles"], help="Result array sorting metric")

args = parser.parse_args()

# =========================================================================
# RESOLVE PARAMETERS: DUAL-MODE ROUTING (FLAGS VS. INTERACTIVE PROMPTS)
# =========================================================================

# 1. Target Search Term Query
search_term = args.search if args.search else Prompt.ask("[bold cyan][?][/bold cyan] Enter search term")

# 2. Search Mode Routing
if args.mode:
    url_mode = SEARCH_MODES[args.mode]
else:
    mode_choice = Prompt.ask(
        "[bold cyan][?][/bold cyan] Select search mode", 
        choices=["broad", "strict", "fuzzy", "substring", "whole word"], 
        default="broad"
    ).lower()
    url_mode = SEARCH_MODES[mode_choice]

# 3. Query Target Size Selection
if args.per:
    url_per = args.per
else:
    url_per = IntPrompt.ask(
        "[bold cyan][?][/bold cyan] Results per page", 
        choices=[str(c) for c in VALID_COUNTS], 
        default=20
    )

# 4. Result Sorting Scheme Selection
if args.sort:
    url_sort = SORT_TYPES[args.sort]
else:
    sort_choice = Prompt.ask(
        "[bold cyan][?][/bold cyan] Sort by", 
        choices=["latest", "oldest", "most files"], 
        default="latest"
    ).lower()
    url_sort = SORT_TYPES[sort_choice]

# =========================================================================
# SAFE URL ASSEMBLY
# =========================================================================
base_url = "https://balbums.st/"
query_params = {
    "search": search_term,
    "mode": url_mode,
    "per": url_per,
    "sort": url_sort
}

# Safely encode special chars/spaces (e.g., ' ' -> '+')
final_search_url = f"{base_url}?{urllib.parse.urlencode(query_params)}"

console.print(f"\n[bold yellow][*][/bold yellow] STEP 1: Loading search results from: [dim white]{final_search_url}[/dim white]...\n")

# -------------------------------------------------------------------------
# [!] DEVELOPER NOTE: Pass 'final_search_url' directly to curl_cffi here.
# For demonstration, 'scraped_albums' acts as your parsed data array output.
# -------------------------------------------------------------------------
# scraped_albums = your_html_parser_function(client.get(final_search_url).text)


# =========================================================================
# PAGINATED CONSOLE LAYOUT GENERATOR (STRICT MAX 10 ROWS PER VIEW)
# =========================================================================
def display_paginated_results(scraped_albums):
    total_found = len(scraped_albums)
    console.print(f"[bold green][+][/bold green] Total scraped payload size: {total_found} entries.")
    
    PAGE_SIZE = 10

    for start_idx in range(0, total_found, PAGE_SIZE):
        end_idx = start_idx + PAGE_SIZE
        chunk = scraped_albums[start_idx:end_idx]
        
        # Instantiate localized grid style for current layout chunk
        table = Table(
            title=f"\n[bold cyan]Displaying Results {start_idx + 1} to {min(end_idx, total_found)}[/bold cyan]", 
            title_justify="left", 
            style="dim white"
        )
        
        table.add_column("#", justify="right", style="magenta", no_wrap=True)
        table.add_column("Album Title", style="white")
        table.add_column("Files", justify="center", style="green")
        table.add_column("URL", style="blue")
        
        # Hydrate Table rows
        for i, album in enumerate(chunk, start=start_idx + 1):
            table.add_row(
                str(i), 
                album.get("title", "Unknown"), 
                f"{album.get('files', 0)} files", 
                album.get("url", "")
            )
        
        console.print(table)
        
        # Enforce step break if upcoming items remain beyond the current viewport matrix
        if end_idx < total_found:
            console.print(f"\n[yellow][*][/yellow] Chunk view paused ({min(end_idx, total_found)}/{total_found}). Press [bold white]Enter[/bold white] to view next chunk...", end="")
            input()