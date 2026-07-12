import re
import sys
import argparse
import asyncio
import json
import urllib.parse
from curl_cffi.requests import AsyncSession
from curl_cffi.curl import CurlError  # Catch connection reset/TLS layer exceptions
from bs4 import BeautifulSoup

# Import rich components for UI rendering
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt

console = Console()

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

VALID_COUNTS = [20, 40, 60, 100]

def parse_arguments():
    """Parse command line arguments for the advanced script"""
    parser = argparse.ArgumentParser(description="Album search and deep parser utility.")
    parser.add_argument("search", nargs="?", help="The targeted search term query string")
    parser.add_argument("-m", "--mode", choices=["broad", "strict", "fuzzy", "substring", "whole"], help="Filter execution mode")
    parser.add_argument("-p", "--per", type=int, choices=VALID_COUNTS, help="Total results requested per engine execution")
    parser.add_argument("-s", "--sort", choices=["latest", "oldest", "mostfiles"], help="Result array sorting metric")
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
                raise e
            console.print(f"  [bold yellow][!][/bold yellow] Network glitch caught ({e}). Retrying in {delay}s... (Attempt {attempt}/{retries})")
            await asyncio.sleep(delay)

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
            parsed_files.append({
                'slug_id': file_meta.get('id', None),
                'href': f"https://bunkr.cr/f/{file_meta.get('name', '')}" if 'name' in file_meta else None,
                'title': file_meta.get('name', 'Unknown Title'),
                'size': f"{round(file_meta['size'] / (1024*1024), 2)} MB" if 'size' in file_meta else None,
                'true_file_id': file_meta.get('id', None),
                **{k: v for k, v in file_meta.items() if k not in ['id', 'name']}
            })
            
    return parsed_files

def slugify_filename(idx, title):
    """Sanitize the album title and prepend the selection index number"""
    clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
    clean_title = re.sub(r'\s+', "_", clean_title).strip("_")
    base_name = clean_title if clean_title else "album_output"
    return f"{idx}_{base_name}"

def display_paginated_results_and_choose(albums):
    """Render results using rich table pagination and accept input choice. Returns (album, choice_num)"""
    total_found = len(albums)
    PAGE_SIZE = 10
    
    for start_idx in range(0, total_found, PAGE_SIZE):
        end_idx = start_idx + PAGE_SIZE
        chunk = albums[start_idx:end_idx]
        
        table = Table(
            title=f"\n[bold cyan]Found Albums ({start_idx + 1} to {min(end_idx, total_found)} of {total_found})[/bold cyan]", 
            title_justify="left", 
            style="dim white"
        )
        table.add_column("#", justify="right", style="magenta", no_wrap=True)
        table.add_column("Album Title", style="white")
        table.add_column("Files", justify="center", style="green")
        table.add_column("URL", style="blue")
        
        for i, album in enumerate(chunk, start=start_idx + 1):
            table.add_row(
                str(i), 
                album.get("title", "Unknown")[:60], 
                album.get("file_count", "0 files"), 
                album.get("url", "")
            )
        
        console.print(table)
        
        is_last_page = end_idx >= total_found
        prompt_text = (
            f"\n[bold cyan][?][/bold cyan] Enter album number (1-{total_found}) "
            f"or [bold white]Enter[/bold white] for next page (or [bold red]q[/bold red] to quit)" if not is_last_page else 
            f"\n[bold cyan][?][/bold cyan] Enter album number (1-{total_found}) (or [bold red]q[/bold red] to quit)"
        )
        
        choice = Prompt.ask(prompt_text, default="").strip().lower()
        
        if choice in ['q', 'quit', 'exit']:
            console.print("[bold yellow][*][/bold yellow] Session cancelled by user.")
            return None, None
            
        # Catch accidental 'Enter' inputs on the final page and offer a second chance
        if not choice and is_last_page:
            console.print("[bold yellow][!][/bold yellow] You reached the end of the list. Please select a valid index number to parse.")
            choice = Prompt.ask(f"[bold cyan][?][/bold cyan] Enter album number (1-{total_found})", default="").strip().lower()
            if choice in ['q', 'quit', 'exit']:
                console.print("[bold yellow][*][/bold yellow] Session cancelled by user.")
                return None, None

        if choice:
            try:
                choice_idx = int(choice) - 1
                if 0 <= choice_idx < total_found:
                    return albums[choice_idx], choice_idx + 1
                else:
                    console.print("[bold red][-][/bold red] Choice index is out of bounds.")
                    return None, None
            except ValueError:
                console.print("[bold red][-][/bold red] Invalid selection digit.")
                return None, None
                
    console.print("[bold red][-][/bold red] End of results reached without selection.")
    return None, None

async def run_scraper():
    args = parse_arguments()
    
    search_term = args.search if args.search else Prompt.ask("[bold cyan][?][/bold cyan] Enter search term").strip()
    if not search_term:
        console.print("[bold red][-][/bold red] Error: Search term cannot be empty.")
        sys.exit(1)

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

    async with AsyncSession(impersonate="chrome") as session:
        try:
            query_params = {
                'search': search_term,
                'mode': url_mode,
                'per': str(url_per),
                'sort': url_sort
            }
            search_url = f"https://balbums.st/?{urllib.parse.urlencode(query_params)}"
            console.print(f"\n[bold yellow][*][/bold yellow] STEP 1: Loading search results from: [dim white]{search_url}[/dim white]...\n")
            
            res = await fetch_with_retry_async(session, search_url)
            
            albums = parse_albums_from_html(res.text)
            if not albums:
                console.print("[bold red][-][/bold red] No albums found matching your query criteria!")
                return
            
            selected_album, album_number = display_paginated_results_and_choose(albums)
            if not selected_album:
                return
            
            console.print(f"\n[bold green][+][/bold green] Selected: #[bold yellow]{album_number}[/bold yellow] - [bold white]{selected_album['title']}[/bold white]")
            
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
                size_str = f" ({f_rec['size']})" if f_rec['size'] else ""
                console.print(f"  {i}. {f_rec['title']}{size_str}{id_str}")
            
            results = {
                'search_term': search_term,
                'selected_album': {
                    **selected_album,
                    'album_index_number': album_number,
                    'aggregate_size': album_size,
                    'clean_file_count': total_files if total_files else f"{len(final_files)} files"
                },
                'files_found': final_files
            }
            
            output_filename = f"{slugify_filename(album_number, selected_album['title'])}.json"
            with open(output_filename, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            console.print(f"\n[bold green][+][/bold green] Enriched results saved out to [bold white]{output_filename}[/bold white]")
            
        except Exception as e:
            console.print(f"[bold red][-][/bold red] Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_scraper())