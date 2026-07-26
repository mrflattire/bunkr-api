# scrape.py
import argparse
import asyncio
import json
import re
import urllib.parse

from bs4 import BeautifulSoup

# Core database and utility engine imports
from core import DatabaseManager
from curl_cffi.curl import CurlError  # Catch connection reset/TLS layer exceptions
from curl_cffi.requests import AsyncSession

# Import rich components for UI rendering
from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from utils import slugify_filename

console = Console()
db = DatabaseManager()

# =========================================================================
# CONFIGURATION & MAPPING DICTIONARIES
# =========================================================================
SEARCH_MODES = {
    "broad": "broad",
    "strict": "strict",
    "fuzzy": "fuzzy",
    "substring": "substring",
    "whole": "whole"
}

SORT_TYPES = {
    "latest": "latest",
    "oldest": "oldest",
    "most files": "mostfiles",
    "mostfiles": "mostfiles"
}

TOP_CATEGORIES = {
    "albums": "topalbums",
    "videos": "topvideos",
    "files": "topfiles",
    "images": "topimages"
}

VALID_COUNTS = [20, 40, 60, 100]

def parse_arguments():
    """Parse command line arguments for the advanced script"""
    parser = argparse.ArgumentParser(description="Album search and deep parser utility.")
    parser.add_argument("search", nargs="?", default=None, help="The targeted search term query string")
    parser.add_argument("-m", "--mode", choices=["broad", "strict", "fuzzy", "substring", "whole"], help="Filter execution mode")
    parser.add_argument("-p", "--per", type=int, choices=VALID_COUNTS, help="Total results requested per engine execution")
    parser.add_argument("-s", "--sort", choices=["latest", "oldest", "mostfiles"], help="Result array sorting metric")
    
    # Flexible Switch: Allows --top on its own (const="prompt") or with an exact choice
    parser.add_argument("-t", "--top", nargs="?", const="prompt", default=None,
                        help="Bypass standard search/homepage view and crawl specific trending layout categories directly")
    
    # Opt-in for local JSON dump (defaults to False)[cite: 5]
    parser.add_argument("--save-json", action="store_true", help="Save a local backup JSON payload of the scraped album")
    return parser.parse_args()

async def fetch_with_retry_async(session, url, retries=3, delay=1, timeout=30):
    """Helper method to execute GET requests with up to 3 automated retries upon CurlError 35 failures"""
    for attempt in range(1, retries + 1):
        try:
            res = await session.get(url, timeout=timeout)
            res.raise_for_status()
            return res
        except CurlError as e:
            if attempt == retries:
                raise
            console.print(f"  [bold yellow][!][/bold yellow] Network glitch caught ({e}). Retrying in {delay}s... (Attempt {attempt}/{retries})")
            await asyncio.sleep(delay)

def standardize_top_url(url: str) -> str:
    """Converts specific /v/ and /i/ redirect configurations to /f/ to pass through deep landing page selectors"""
    return re.sub(r'/(v|i)/', '/f/', url)

def parse_albums_from_html(html):
    """Extract album information and file counts from search results page"""
    soup = BeautifulSoup(html, 'html.parser')
    albums = []
    
    for card in soup.find_all('a', href=True):
        href = card.get('href', '')
        
        if re.search(r'/a/[\w-]+', href):
            title_tag = card.find('h3')
            title = title_tag.get_text(strip=True) if title_tag else card.get_text(strip=True)
            
            file_count = None
            span_tags = card.find_all('span')
            for span in span_tags:
                span_text = span.get_text(strip=True)
                if 'file' in span_text:
                    file_count = span_text
                    break
            
            if title:
                albums.append({
                    'title': title,
                    'url': href if href.startswith('http') else 'https://bunkr.cr' + href,
                    'file_count': file_count if file_count else "0 files"
                })
    
    seen = set()
    unique_albums = []
    for album in albums:
        if album['url'] not in seen:
            unique_albums.append(album)
            seen.add(album['url'])
    
    return unique_albums

def parse_top_items_from_html(html, category):
    """Extract and isolate trending listings with clean link conversions applied to asset types"""
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    
    # Establish dynamic routing markers
    prefix_map = {"albums": "a", "videos": "v", "files": "f", "images": "i"}
    target_prefix = prefix_map.get(category, "f")
    
    for card in soup.find_all('a', href=True):
        href = card.get('href', '')
        
        if re.search(rf'/{target_prefix}/[\w-]+', href):
            title_tag = card.find('h3') or card.find('p')
            title = title_tag.get_text(strip=True) if title_tag else card.get_text(strip=True)
            
            full_url = href if href.startswith('http') else 'https://bunkr.cr' + href
            
            # Apply URL standardization path corrections to avoid broken redirections downstream
            if category in ["videos", "images"]:
                full_url = standardize_top_url(full_url)
                
            file_count = None
            span_tags = card.find_all('span')
            for span in span_tags:
                span_text = span.get_text(strip=True)
                if 'file' in span_text:
                    file_count = span_text
                    break
            
            if title:
                items.append({
                    'title': title,
                    'url': full_url,
                    'file_count': file_count if file_count else "1 file"
                })
                
    # Retain strictly ordered unique values
    seen = set()
    return [x for x in items if not (x['url'] in seen or seen.add(x['url']))]

def extract_page_metadata(soup):
    """Parses total global pages from the HTML pagination section at the bottom of the page"""
    footer_div = soup.find('div', class_='text-xs text-[var(--text-soft)] mono')
    if footer_div:
        text = footer_div.get_text(strip=True)
        match = re.search(r'Page\s+\d+\s+of\s+(\d+)', text, re.IGNORECASE)
        if match:
            return match.group(1)
            
    top_span = soup.find('span', class_='text-[var(--text)]')
    if top_span:
        parent_text = top_span.parent.get_text(strip=True) if top_span.parent else ""
        match = re.search(r'page\s+\d+\s+of\s+(\d+)', parent_text, re.IGNORECASE)
        if match:
            return match.group(1)
            
    return "Unknown"

def parse_album_metadata(soup):
    """Extract full album size and total files from the visitors paragraph"""
    album_size = None
    total_files = None
    
    size_el = soup.select_one(".visitors .font-semibold")
    if size_el:
        text = size_el.get_text(strip=True)
        size_match = re.search(r'\((.*?)\)', text)
        if size_match:
            album_size = size_match.group(1)
        
        files_match = re.search(r'\)\s*(.*)', text)
        if files_match:
            total_files = files_match.group(1).strip()
            
    return album_size, total_files

def extract_advanced_album_files(html_content: str) -> list:
    """Extracts and parses the window.albumFiles array directly from the advanced layout script block"""
    match = re.search(r'window\.albumFiles\s*=\s*\[(.*?)\];', html_content, re.DOTALL)
    if not match:
        return []
        
    array_content = match.group(1)
    object_strings = re.findall(r'\{([^}]+)\}', array_content, re.DOTALL)
    parsed_files = []
    
    for obj_str in object_strings:
        file_meta = {}
        matches = re.findall(r'(\w+):\s*(?:"([^"]*)"|(\d+))', obj_str)
        
        for key, str_val, num_val in matches:
            if num_val:
                file_meta[key] = int(num_val)
            else:
                file_meta[key] = str_val
                
        if file_meta:
            # The site's own rendering JS (createFileItem, copyList) confirms the
            # real schema: file.slug builds the /f/ URL, file.original is the
            # display filename. Neither is 'name' — that field was silently
            # wrong for both href and title. Falling back to 'name' only if
            # 'slug'/'original' are somehow absent, so this degrades instead
            # of breaking outright if the site's structure shifts again.
            slug = file_meta.get('slug') or file_meta.get('name')
            display_name = file_meta.get('original') or file_meta.get('name', 'Unknown Title')

            parsed_files.append({
                'slug_id': file_meta.get('id', None),
                'href': f"https://bunkr.cr/f/{urllib.parse.quote(str(slug))}" if slug else None,
                'title': display_name,
                'original': display_name,
                'size': file_meta.get('size', 0), # Store pure raw integer bytes directly for database compatibility
                'true_file_id': file_meta.get('id', None),
                **{k: v for k, v in file_meta.items() if k not in ['id', 'name', 'slug', 'original', 'size']}
            })
            
    return parsed_files

def display_current_page_and_choose(albums, current_page, total_pages, search_term, mode, context_type="search", per_page=20):
    """Renders the current batch of items with unified context status headers and accurate sequential row counts across pages"""
    total_loaded = len(albums)
    
    # Calculate the precise start and end index limits matching background state counters
    start_index = ((current_page - 1) * per_page) + 1
    end_index = start_index + total_loaded - 1
    
    if context_type == "top":
        header_title = f"Top Trending {search_term.capitalize()} Page {current_page} (Items {start_index}-{end_index} loaded)"
    else:
        display_search = f'"{search_term}"' if search_term else '"Homepage"'
        header_title = f"{display_search} Results Page {current_page} of {total_pages} (Items {start_index}-{end_index} loaded) Mode: {mode}"
    
    table = Table(
        title=f"\n[bold cyan]{header_title}[/bold cyan]", 
        title_justify="left", 
        style="dim white"
    )
    table.add_column("#", justify="right", style="magenta", no_wrap=True)
    table.add_column("Title / Target Item", style="white")
    table.add_column("Files (Est.)", justify="center", style="green")
    table.add_column("Source Page Link", style="blue")
    
    # Inject start_index directly into the visual enumeration routine
    for i, album in enumerate(albums, start=start_index):
        table.add_row(
            str(i), 
            album.get("title", "Unknown")[:60], 
            album.get("file_count", "0 files"), 
            album.get("url", "")
        )
    
    console.print(table)
    
    prompt_text = (
        f"\n[bold cyan][?][/bold cyan] Enter selection number ({start_index}-{end_index}), "
        f"[bold white]'n'[/bold white] for next page, "
        f"[bold white]'p'[/bold white] for previous page (or [bold red]q[/bold red] to quit)"
    )
    
    choice = Prompt.ask(prompt_text, default="").strip().lower()
    return choice

async def run_top_engine(session, category: str, save_json: bool = False):
    """Isolated sequence running exclusive algorithms for trending media lists without regressions"""
    lapse_choice = Prompt.ask(
        "[bold cyan][?][/bold cyan] Select trending timeframe", 
        choices=["24h", "7d", "30d", "all"], 
        default="24h"
    ).lower()
    
    current_page = 1
    selected_item = None
    item_number_index = None
    
    while True:
        if current_page == 1:
            top_url = f"https://balbums.st/{TOP_CATEGORIES[category]}?lapse={lapse_choice}"
        else:
            top_url = f"https://balbums.st/{TOP_CATEGORIES[category]}?lapse={lapse_choice}&page={current_page}"
            
        console.print(f"\n[bold yellow][*][/bold yellow] STEP 1: Loading trending array from: [dim white]{top_url}[/dim white]...\n")
        
        res = await fetch_with_retry_async(session, top_url)
        items = parse_top_items_from_html(res.text, category)
        
        if not items:
            console.print(f"[bold red][-][/bold red] No items found on trending page {current_page}!")
            if current_page > 1:
                console.print("[bold yellow][*][/bold yellow] Reverting browser pipeline frame to previous page...")
                current_page -= 1
                await asyncio.sleep(1.5)
                continue
            return
            
        if len(items) < 15:
            console.print("[bold yellow][!][/bold yellow] Partial index sequence flagged. End of list imminent.")
            
        # Top page size limits are locked strictly at 15 items per response frame
        choice = display_current_page_and_choose(items, current_page, "Unknown", lapse_choice, None, context_type="top", per_page=15)
        
        if choice in ['q', 'quit', 'exit']:
            console.print("[bold yellow][*][/bold yellow] Trending browse session closed.")
            return
        elif choice == 'n':
            if len(items) < 15:
                console.print("[bold red][-][/bold red] Cannot advance. End of available data reached.")
                await asyncio.sleep(1.5)
            else:
                current_page += 1
            continue
        elif choice == 'p':
            if current_page > 1:
                current_page -= 1
            else:
                console.print("[bold yellow][!][/bold yellow] Already viewing the first page.")
            continue
        elif choice == '':
            if len(items) < 15:
                console.print("[bold red][-][/bold red] End of list reached.")
                await asyncio.sleep(1.5)
            else:
                current_page += 1
            continue
        else:
            try:
                choice_idx = int(choice)
                start_boundary = ((current_page - 1) * 15) + 1
                end_boundary = start_boundary + len(items) - 1
                
                # Check user entry directly against active global bounding thresholds
                if start_boundary <= choice_idx <= end_boundary:
                    # Convert the absolute global input index safely back to internal relative array slot
                    relative_idx = choice_idx - start_boundary
                    selected_item = items[relative_idx]
                    item_number_index = choice_idx
                    break
                else:
                    console.print(f"[bold red][-][/bold red] Selection out of bounds. Enter a number between {start_boundary}-{end_boundary}.")
            except ValueError:
                console.print("[bold red][-][/bold red] Invalid character command selection.")

    if not selected_item:
        return
        
    await execute_deep_resolution(session, selected_item, item_number_index, f"top_{category}_{lapse_choice}", save_json=save_json)

async def execute_deep_resolution(session, selected_album, album_number_index, search_term, save_json: bool = False):
    """Consolidated module running uniform deep node parsing logic and syncing metadata directly into SQLite"""
    console.print(f"\n[bold green][+][/bold green] Selected: #[bold yellow]{album_number_index}[/bold yellow] - [bold white]{selected_album['title']}[/bold white]")
    
    parsed_album_url = urllib.parse.urlparse(selected_album['url'])
    album_params = dict(urllib.parse.parse_qsl(parsed_album_url.query))
    album_params['advanced'] = '1'
    optimized_url = urllib.parse.urlunparse((
        parsed_album_url.scheme, parsed_album_url.netloc, parsed_album_url.path,
        parsed_album_url.params, urllib.parse.urlencode(album_params), parsed_album_url.fragment
    ))
    
    console.print(f"\n[bold yellow][*][/bold yellow] STEP 2: Navigating to optimized album view: [dim white]{optimized_url}[/dim white]...")
    res = await fetch_with_retry_async(session, optimized_url)
    album_soup = BeautifulSoup(res.text, 'html.parser')
    
    album_size, total_files = parse_album_metadata(album_soup)
    if album_size and total_files:
        console.print(f"[bold green][+][/bold green] Album Info -> Aggregate Size: [bold yellow]{album_size}[/bold yellow] | Count: [bold yellow]{total_files}[/bold yellow]")
    
    final_files = extract_advanced_album_files(res.text)
    console.print(f"[bold green][+][/bold green] Deep resolution complete. Pulled {len(final_files)} accurate item profiles instantly.")
    
    for i, f_rec in enumerate(final_files[:10], start=1):
        id_str = f" [True ID: {f_rec['true_file_id']}]" if f_rec['true_file_id'] else " [ID: Not Found]"
        # Check size field safely
        raw_size = f_rec.get('size', 0)
        size_str = f" ({round(raw_size / (1024*1024), 2)} MB)" if raw_size else ""
        console.print(f"  {i}. {f_rec['title']}{size_str}{id_str}")
    
    # Package JSON payload context matching structural requirements for register_album_from_json()[cite: 5]
    results = {
        'search_term': search_term,
        'selected_album': {
            'title': selected_album['title'],
            'album_index_number': album_number_index,
            'aggregate_size': 0 if not album_size else album_size, # Let core map or fallback
            'clean_file_count': total_files if total_files else f"{len(final_files)} files"
        },
        'files_found': final_files
    }
    
    # Conditionally write JSON based on save_json status[cite: 5]
    if save_json:
        output_filename = f"{slugify_filename(album_number_index, selected_album['title'])}.json"
        with open(output_filename, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        console.print(f"[bold green][+][/bold green] Enriched results saved out to [bold white]{output_filename}[/bold white]")

    # SQLite sync: Safely sync via our unified Core API to avoid DB schema collisions[cite: 5]
    try:
        console.print("[bold yellow][*][/bold yellow] Syncing records with Database Manager...")
        album_id = db.register_album_from_json(results) # Beautifully delegated[cite: 5]
        console.print(f"[bold green][+][/bold green] Database synchronization complete! Registered Album ID: [bold yellow]#{album_id}[/bold yellow]")
    except Exception as e:
        console.print(f"[bold red][-][/bold red] Database synchronization failed: {e}")

async def run_scraper():
    args = parse_arguments()
    save_json = args.save_json  # Preserve active context state[cite: 5]
    
    async with AsyncSession(impersonate="chrome") as session:
        # Check bypass flag condition to keep core flow pristine
        if args.top is not None:
            category = args.top
            
            if category == "prompt":
                category = Prompt.ask(
                    "[bold cyan][?][/bold cyan] Select trending category", 
                    choices=["albums", "videos", "files", "images"], 
                    default="albums"
                ).lower()
            elif category not in ["albums", "videos", "files", "images"]:
                console.print(f"[bold red][-][/bold red] Invalid top category: '{category}'. Valid: albums, videos, files, images.")
                return

            await run_top_engine(session, category, save_json=save_json)
            return

        # =========================================================================
        # STANDARD SEARCH / HOMEPAGE MAIN PIPELINE VIEW
        # =========================================================================
        if args.search is not None:
            search_term = args.search
        else:
            search_term = Prompt.ask("[bold cyan][?][/bold cyan] Enter search term [dim](Leave blank for homepage layout)[/dim]").strip()

        if args.mode:
            url_mode = SEARCH_MODES[args.mode]
        else:
            mode_choice = Prompt.ask(
                "[bold cyan][?][/bold cyan] Select search mode", 
                choices=["broad", "strict", "fuzzy", "substring", "whole"], 
                default="broad"
            ).lower()
            url_mode = SEARCH_MODES[mode_choice]

        if args.per:
            url_per = args.per
        else:
            url_per = IntPrompt.ask(
                "[bold cyan][?][/bold cyan] Results per page", 
                choices=[str(c) for c in VALID_COUNTS], 
                default=20
            )

        if args.sort:
            url_sort = SORT_TYPES[args.sort]
        else:
            sort_choice = Prompt.ask(
                "[bold cyan][?][/bold cyan] Sort by", 
                choices=["latest", "oldest", "most files"], 
                default="latest"
            ).lower()
            url_sort = SORT_TYPES[sort_choice]

        current_page = 1
        selected_album = None
        album_number_index = None

        try:
            while True:
                query_params = {
                    'search': search_term,
                    'mode': url_mode,
                    'per': str(url_per),
                    'sort': url_sort
                }
                
                if current_page > 1:
                    query_params['page'] = str(current_page)

                search_url = f"https://balbums.st/?{urllib.parse.urlencode(query_params)}"
                console.print(f"\n[bold yellow][*][/bold yellow] STEP 1: Loading structural items from: [dim white]{search_url}[/dim white]...\n")
                
                res = await fetch_with_retry_async(session, search_url)
                soup = BeautifulSoup(res.text, 'html.parser')
                
                albums = parse_albums_from_html(res.text)
                total_pages = extract_page_metadata(soup)
                
                if not albums:
                    console.print(f"[bold red][-][/bold red] No albums found on page {current_page}!")
                    if current_page > 1:
                        console.print("[bold yellow][*][/bold yellow] Reverting layout to the previous valid active page frame context...")
                        current_page -= 1
                        await asyncio.sleep(1.5)
                        continue
                    return
                
                # Pass the custom dynamic per_page calculation directly to mirror table elements
                choice = display_current_page_and_choose(albums, current_page, total_pages, search_term, url_mode, context_type="search", per_page=url_per)
                
                if choice in ['q', 'quit', 'exit']:
                    console.print("[bold yellow][*][/bold yellow] Session cancelled by user.")
                    return
                elif choice == 'n':
                    current_page += 1
                    continue
                elif choice == 'p':
                    if current_page > 1:
                        current_page -= 1
                    else:
                        console.print("[bold yellow][!][/bold yellow] You are already on the first page.")
                    continue
                elif choice == '':
                    current_page += 1
                    continue
                else:
                    try:
                        choice_idx = int(choice)
                        start_boundary = ((current_page - 1) * url_per) + 1
                        end_boundary = start_boundary + len(albums) - 1
                        
                        if start_boundary <= choice_idx <= end_boundary:
                            relative_idx = choice_idx - start_boundary
                            selected_album = albums[relative_idx]
                            album_number_index = choice_idx
                            break
                        else:
                            console.print(f"[bold red][-][/bold red] Selection out of bounds. Enter a number between {start_boundary}-{end_boundary}.")
                    except ValueError:
                        console.print("[bold red][-][/bold red] Invalid command options entered.")

            if not selected_album:
                return
                
            await execute_deep_resolution(session, selected_album, album_number_index, search_term, save_json=save_json)
            
        except Exception as e:
            console.print(f"[bold red][-][/bold red] Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_scraper())