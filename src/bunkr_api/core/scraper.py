import argparse
import asyncio
import json
import re
import sys
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup
from rich.console import Console

from ..config import (
    DEFAULT_OUTPUT_DIR,
    HEADERS,
    SEARCH_MODES,
    VALID_COUNTS,
)
from ..utils.formatting import slugify_filename
from ..utils.http import execute_request_with_retry_async

console = Console()

class ScraperEngine:
    def __init__(self, db):
        """
        Initializes the scraper engine with a shared database instance.
        """
        self.db = db

    def _safe_int(self, val):
        """Helper to prevent ValueError crashes during parsing of sizes or counts."""
        if isinstance(val, int):
            return val
        if not val:
            return 0
        try:
            clean_val = re.sub(r'[^\d]', '', str(val))
            return int(clean_val) if clean_val else 0
        except Exception:
            return 0

    def standardize_top_url(self, url: str) -> str:
        """Converts specific /v/ and /i/ redirect configurations to /f/."""
        return re.sub(r'/(v|i)/', '/f/', url)

    def parse_albums(self, html):
        """Extract album information and file counts from search results page."""
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
                    if 'file' in span_text.lower():
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

    def parse_top_items(self, html, category):
        """Extract and isolate trending listings for the 'Trending' CLI feature."""
        soup = BeautifulSoup(html, 'html.parser')
        items = []
        prefix_map = {"albums": "a", "videos": "v", "files": "f", "images": "i"}
        target_prefix = prefix_map.get(category, "f")
        
        for card in soup.find_all('a', href=True):
            href = card.get('href', '')
            if re.search(rf'/{target_prefix}/[\w-]+', href):
                title_tag = card.find('h3') or card.find('p')
                title = title_tag.get_text(strip=True) if title_tag else card.get_text(strip=True)
                full_url = href if href.startswith('http') else 'https://bunkr.cr' + href
                
                if category in ["videos", "images"]:
                    full_url = self.standardize_top_url(full_url)
                
                file_count = "1 file"
                for span in card.find_all('span'):
                    if 'file' in span.get_text(strip=True).lower():
                        file_count = span.get_text(strip=True)
                        break
                if title:
                    items.append({'title': title, 'url': full_url, 'file_count': file_count})
        seen = set()
        return [x for x in items if not (x['url'] in seen or seen.add(x['url']))]

    def extract_page_metadata(self, soup):
        """Parses total global pages from the HTML pagination section of a search results page."""
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

    def parse_album_header_metadata(self, soup):
        """Extract full album size and total files from the visitors paragraph."""
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

    def extract_advanced_album_files(self, html_content: str) -> list:
        """RESTORED: Exact regex and logic to handle 'slug' vs 'name' vs 'original'."""
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
                slug = file_meta.get('slug') or file_meta.get('name')
                display_name = file_meta.get('original') or file_meta.get('name', 'Unknown Title')

                parsed_files.append({
                    'true_file_id': file_meta.get('id'),
                    'slug_id': file_meta.get('id'), 
                    'href': f"https://bunkr.cr/f/{urllib.parse.quote(str(slug))}" if slug else None,
                    'title': display_name,
                    'original': display_name,
                    'size': self._safe_int(file_meta.get('size', 0)),
                    **{k: v for k, v in file_meta.items() if k not in ['id', 'name', 'slug', 'original', 'size']}
                })
                
        return parsed_files

    async def scrape_album(self, session, url, search_term, album_number_index=0, save_json=False, output_dir=None):
        """Deep resolution of an album with original informative console logging."""
        parsed_url = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed_url.query))
        params['advanced'] = '1'
        optimized_url = urllib.parse.urlunparse(
            parsed_url._replace(query=urllib.parse.urlencode(params))
        )
        
        console.print("\n[bold yellow][*][/bold yellow] Navigating optimized album view ")
        
        res = await execute_request_with_retry_async(session, optimized_url, headers=HEADERS)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        album_title = (
            re.sub(r'\s*[\|\-]\s*Bunkr\s*$', '', soup.title.string, flags=re.IGNORECASE)
            if soup.title else "Unknown Album"
        )
        
        console.print(f"\n[bold green][+][/bold green] Selected: #[bold yellow]{album_number_index}[/bold yellow] - [bold white]{album_title}[/bold white]")

        album_size, total_files = self.parse_album_header_metadata(soup)
        final_files = self.extract_advanced_album_files(res.text)
        
        if album_size and total_files:
            console.print(f"[bold green][+][/bold green] Album Info -> Aggregate Size: [bold yellow]{album_size}[/bold yellow] | Count: [bold yellow]{total_files}[/bold yellow]")
        
        console.print(f"[bold green][+][/bold green] Deep resolution complete. Pulled [bold cyan]{len(final_files)}[/bold cyan] accurate item profiles instantly.")

        for i, f_rec in enumerate(final_files[:10], start=1):
            raw_size = f_rec.get('size', 0)
            size_mb = round(raw_size / (1024*1024), 2)
            size_str = f" ({size_mb} MB)" if raw_size else ""
            console.print(f"  {i}. {f_rec['title']}{size_str} [[italic green]True[/italic green] ID: {f_rec['true_file_id']}]")

        # Stable identity for this album, independent of its position in any
        # given search results page (which shifts between scrapes and previously
        # caused re-scrapes to register as phantom duplicate albums).
        slug_match = re.search(r'/a/([\w-]+)', url)
        album_slug = slug_match.group(1) if slug_match else None

        data = {
            'search_term': search_term or "N/A",
            'selected_album': {
                'title': album_title.strip(),
                'album_index_number': album_number_index,
                'album_slug': album_slug,
                'album_url': url,
                'aggregate_size': album_size if album_size else "0 MB",
                'clean_file_count': total_files if total_files else f"{len(final_files)} files"
            },
            'files_found': final_files
        }
        
        if save_json:
            # Determine correct output directory
            save_path = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
            save_path.mkdir(parents=True, exist_ok=True)
            
            output_filename = save_path / f"{slugify_filename(album_number_index, album_title)}.json"
            try:
                payload = json.dumps(data, indent=2, ensure_ascii=False)
                await asyncio.to_thread(output_filename.write_text, payload, encoding="utf-8")
                console.print(f"[bold green][+][/bold green] Enriched results saved out to [bold white]{output_filename}[/bold white]")
            except (OSError, TypeError, ValueError) as e:
                console.print(f"[red][!] JSON Backup Failed: {e}[/red]")

        console.print("[bold yellow][*][/bold yellow] Syncing records with Database Manager...")
        album_id, new_count, updated_count = self.db.register_album_from_json(data)

        if new_count and updated_count:
            console.print(
                f"[bold green][+][/bold green] Synced Album ID [bold yellow]#{album_id}[/bold yellow] — "
                f"[bold cyan]{new_count}[/bold cyan] new file(s), [bold cyan]{updated_count}[/bold cyan] refreshed."
            )
        elif new_count:
            console.print(
                f"[bold green][+][/bold green] Registered Album ID [bold yellow]#{album_id}[/bold yellow] with "
                f"[bold cyan]{new_count}[/bold cyan] file(s)."
            )
        else:
            console.print(
                f"[bold green][+][/bold green] Album ID [bold yellow]#{album_id}[/bold yellow] already up to date "
                f"— [bold cyan]{updated_count}[/bold cyan] file(s) refreshed, no new files found."
            )

        return album_id

def main():
    """Standalone CLI entry point for bunkr-scrape command."""
    parser = argparse.ArgumentParser(description="Bunkr Standalone Scraper CLI")
    parser.add_argument("search", nargs="?", default=None, help="The search query")
    parser.add_argument("-m", "--mode", choices=list(SEARCH_MODES.keys()), help="Search mode")
    parser.add_argument("-p", "--per", type=int, choices=VALID_COUNTS, help="No. of results per page")
    parser.add_argument("-s", "--sort", choices=["latest", "oldest", "mostfiles"], help="Sort by")
    parser.add_argument("-t", "--top", nargs="?", const="prompt", help="Trending category")
    parser.add_argument("--save-json", action="store_true", help="Save backup JSON")
    parser.add_argument("-o", "--output", type=str, default=None, help="Output directory for a JSON metadata")
    
    args = parser.parse_args()

    url_mode = SEARCH_MODES.get(args.mode) if args.mode else None
    
    from ..cli import run_scrape_interactive, run_top_engine_interactive

    async def _run():
        if args.top:
            await run_top_engine_interactive(
                category_seed=args.top if args.top != "prompt" else None,
                save_json_seed=args.save_json,
                output_dir_seed=args.output
            )
        else:
            await run_scrape_interactive(
                search_seed=args.search, 
                mode_seed=url_mode, 
                per_seed=args.per, 
                sort_seed=args.sort,
                save_json_seed=args.save_json,
                output_dir_seed=args.output
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()