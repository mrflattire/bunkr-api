import argparse
import asyncio
import json
import re
import sys
import urllib.parse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

# Import rich components for UI rendering
from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

console = Console()

# =========================================================================
# CONFIGURATION & MAPPING DICTIONARIES
# =========================================================================
SEARCH_MODES = {
    "broad": "broad",
    "strict": "strict",
    "fuzzy": "fuzzy",
    "substring": "substring",
    "whole": "whole"  # Simplified key for unified shortcut access
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

def parse_files_from_album(soup):
    """Extract initial file information and inline details from album page layout"""
    files = []
    items = soup.find_all('div', class_='theItem')
    
    for item in items:
        link_tag = item.find('a', href=True, attrs={'aria-label': 'download'})
        if not link_tag:
            link_tag = item.find('a', href=re.compile(r'/f/'))
            
        if link_tag:
            href = link_tag.get('href', '')
            id_match = re.search(r'/f/([\w\d]+)', href)
            slug_id = id_match.group(1) if id_match else None
            
            name_tag = item.find('p', class_='theName')
            title = name_tag.get_text(strip=True) if name_tag else "Unknown Title"
            
            size_tag = item.find('span', class_='theSize') or item.find(string=re.compile(r'\b(MB|GB|KB)\b'))
            size_str = size_tag.get_text(strip=True) if size_tag else None
            
            files.append({
                'slug_id': slug_id,
                'href': href if href.startswith('http') else 'https://bunkr.cr' + href,
                'title': title,
                'size': size_str,
                'true_file_id': None
            })
            
    if not files:
        for link in soup.find_all('a', href=re.compile(r'/f/')):
            href = link.get('href', '')
            id_match = re.search(r'/f/([\w\d]+)', href)
            if id_match:
                files.append({
                    'slug_id': id_match.group(1),
                    'href': href if href.startswith('http') else 'https://bunkr.cr' + href,
                    'title': link.get_text(strip=True) or "Link File",
                    'size': None,
                    'true_file_id': None
                })
                
    return files

def slugify_filename(title):
    """Sanitize the album title to make it a safe Windows/Linux filename"""
    clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
    clean_title = re.sub(r'\s+', "_", clean_title).strip("_")
    return clean_title if clean_title else "album_output"


def display_paginated_results_and_choose(albums):
    """Render results using rich table pagination max 10 rows and accept input choice"""
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
        
        # Prompt option logic at the footer of each block matrix
        prompt_text = (
            f"\n[bold cyan][?][/bold cyan] Enter album number (1-{total_found}) "
            f"or [bold white]Enter[/bold white] for next page (or [bold red]q[/bold red] to quit)" if end_idx < total_found else 
            f"\n[bold cyan][?][/bold cyan] Enter album number (1-{total_found}) (or [bold red]q[/bold red] to quit)"
        )
        
        choice = Prompt.ask(prompt_text, default="").strip().lower()
        
        if choice in ['q', 'quit', 'exit']:
            console.print("[bold yellow][*][/bold yellow] Session cancelled by user.")
            return None
            
        if choice:
            try:
                choice_idx = int(choice) - 1
                if 0 <= choice_idx < total_found:
                    return albums[choice_idx]
                else:
                    console.print("[bold red][-][/bold red] Choice index is out of bounds.")
                    return None
            except ValueError:
                console.print("[bold red][-][/bold red] Invalid selection digit.")
                return None
                
    console.print("[bold red][-][/bold red] End of results reached without selection.")
    return None


async def run_scraper():
    args = parse_arguments()
    
    # 1. Resolve Target Search Term
    search_term = args.search if args.search else Prompt.ask("[bold cyan][?][/bold cyan] Enter search term").strip()
    if not search_term:
        console.print("[bold red][-][/bold red] Error: Search term cannot be empty.")
        sys.exit(1)

    # 2. Resolve Search Mode Routing
    if args.mode:
        url_mode = SEARCH_MODES[args.mode]
    else:
        mode_choice = Prompt.ask(
            "[bold cyan][?][/bold cyan] Select search mode", 
            choices=["broad", "strict", "fuzzy", "substring", "whole"], 
            default="broad"
        ).lower()
        url_mode = SEARCH_MODES[mode_choice]

    # 3. Resolve Target Size Selection
    if args.per:
        url_per = args.per
    else:
        url_per = IntPrompt.ask(
            "[bold cyan][?][/bold cyan] Results per page", 
            choices=[str(c) for c in VALID_COUNTS], 
            default=20
        )

    # 4. Resolve Result Sorting Scheme Selection
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
            # ====== STEP 1: SEARCH AND PARSE ALBUMS ======
            query_params = {
                'search': search_term,
                'mode': url_mode,
                'per': str(url_per),
                'sort': url_sort
            }
            search_url = f"https://balbums.st/?{urllib.parse.urlencode(query_params)}"
            console.print(f"\n[bold yellow][*][/bold yellow] STEP 1: Loading search results from: [dim white]{search_url}[/dim white]...\n")
            
            res = await session.get(search_url, timeout=30)
            res.raise_for_status()
            
            albums = parse_albums_from_html(res.text)
            if not albums:
                console.print("[bold red][-][/bold red] No albums found matching your query criteria!")
                return
            
            # Display scannable paginated table list matrix (Refactored to regular call)
            selected_album = display_paginated_results_and_choose(albums)
            if not selected_album:
                return
            
            console.print(f"\n[bold green][+][/bold green] Selected: [bold white]{selected_album['title']}[/bold white]")
            
            # ====== STEP 2: NAVIGATE TO ALBUM ======
            console.print(f"\n[bold yellow][*][/bold yellow] STEP 2: Navigating to album via HTTP: [dim white]{selected_album['url']}[/dim white]...")
            res = await session.get(selected_album['url'], timeout=30)
            res.raise_for_status()
            album_soup = BeautifulSoup(res.text, 'html.parser')
            
            # ====== STEP 3: PARSE METADATA & INITIAL FILES FROM ALBUM ======
            console.print("\n[bold yellow][*][/bold yellow] STEP 3: Parsing album metadata and file grid layout...")
            album_size, total_files = parse_album_metadata(album_soup)
            
            if album_size and total_files:
                console.print(f"[bold green][+][/bold green] Album Info -> Aggregate Size: [bold yellow]{album_size}[/bold yellow] | Count: [bold yellow]{total_files}[/bold yellow]")
            
            files = parse_files_from_album(album_soup)
            console.print(f"[bold green][+][/bold green] Found {len(files)} initial items in the album grid.")
            
            # ====== STEP 4: DEEP RESOLUTION OF TRUE FILE IDs & SIZES ======
            console.print("\n[bold yellow][*][/bold yellow] STEP 4: Resolving absolute database IDs and sizes from internal file links...")
            final_files = []
            
            for index, file_item in enumerate(files, start=1):
                console.print(f"  [{index}/{len(files)}] Fetching file landing page for: [dim white]{file_item['title'][:45]}[/dim white]...")
                
                try:
                    file_res = await session.get(file_item['href'], timeout=20)
                    file_res.raise_for_status()
                    file_soup = BeautifulSoup(file_res.text, 'html.parser')
                    
                    tracker = file_soup.find(id="fileTracker")
                    if tracker and tracker.has_attr("data-file-id"):
                        file_item['true_file_id'] = tracker["data-file-id"]
                    else:
                        script_el = file_soup.find("script", attrs={"data-file-id": True})
                        if script_el:
                            file_item['true_file_id'] = script_el["data-file-id"]
                        else:
                            console.print(f"  [red][-][/red] Could not resolve data-file-id for slug: {file_item['slug_id']}")
                    
                    if not file_item['size']:
                        # Fixed: Used 'string=' instead of deprecated 'text=' parameter to prevent warnings
                        size_match = file_soup.find(string=re.compile(r'\b\d+(?:\.\d+)?\s*(?:KB|MB|GB)\b', re.IGNORECASE))
                        if size_match:
                            file_item['size'] = size_match.strip()
                            
                except Exception as sub_err:
                    console.print(f"  [red][-][/red] Connection error reading page {file_item['href']}: {sub_err}")
                
                final_files.append(file_item)
                await asyncio.sleep(0.5)

            # Print clean terminal summary sample layout 
            console.print("\n[bold green][+][/bold green] Deep resolution complete. Sample records saved:")
            for i, f_rec in enumerate(final_files[:10], start=1):
                id_str = f" [True ID: {f_rec['true_file_id']}]" if f_rec['true_file_id'] else " [ID: Not Found]"
                size_str = f" ({f_rec['size']})" if f_rec['size'] else ""
                console.print(f"  {i}. {f_rec['title']}{size_str}{id_str}")
            
            # Save comprehensive results mapping out to file
            results = {
                'search_term': search_term,
                'selected_album': {
                    **selected_album,
                    'aggregate_size': album_size,
                    'clean_file_count': total_files
                },
                'files_found': final_files
            }
            
            output_filename = f"{slugify_filename(selected_album['title'])}.json"
            with open(output_filename, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            console.print(f"\n[bold green][+][/bold green] Enriched results saved out to [bold white]{output_filename}[/bold white]")
            
        except Exception as e:
            console.print(f"[bold red][-][/bold red] Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_scraper())