import re
import json
import asyncio
import time
import urllib.parse
from bs4 import BeautifulSoup
from rich.console import Console

# Internal Package Imports
from ..config import HEADERS
from ..utils.http import execute_request_with_retry_async
from ..utils.formatting import slugify_filename

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
            # Strip non-numeric chars like commas or 'MB'
            clean_val = re.sub(r'[^\d]', '', str(val))
            return int(clean_val) if clean_val else 0
        except (ValueError, TypeError):
            return 0

    def standardize_top_url(self, url: str) -> str:
        """Converts specific /v/ and /i/ redirect configurations to /f/ to pass through deep selectors."""
        return re.sub(r'/(v|i)/', '/f/', url)

    def parse_albums(self, html):
        """
        Extract album information and file counts from search results page.
        """
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
        
        # Unique filter while preserving order
        seen = set()
        unique_albums = []
        for album in albums:
            if album['url'] not in seen:
                unique_albums.append(album)
                seen.add(album['url'])
        
        return unique_albums

    def parse_top_items(self, html, category):
        """
        Extract and isolate trending listings for the 'Trending' CLI feature.
        """
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
                
                file_count = "1 file"
                for span in card.find_all('span'):
                    if 'file' in span.get_text(strip=True).lower():
                        file_count = span.get_text(strip=True)
                        break
                if title:
                    items.append({'title': title, 'url': full_url, 'file_count': file_count})
        seen = set()
        return [x for x in items if not (x['url'] in seen or seen.add(x['url']))]

    def parse_page_metadata(self, html):
        """Parses total global pages from the HTML pagination section."""
        soup = BeautifulSoup(html, 'html.parser')
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
        """
        RESTORED: Exact regex and logic to handle 'slug' vs 'name' vs 'original'.
        Captures raw IDs and metadata required for database synchronization.
        """
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
                # SITE SCHEMA: 
                # slug -> used for the /f/ URL
                # original -> the actual filename with extension
                # id -> the numeric true_file_id
                slug = file_meta.get('slug') or file_meta.get('name')
                display_name = file_meta.get('original') or file_meta.get('name', 'Unknown Title')

                parsed_files.append({
                    'true_file_id': file_meta.get('id'),
                    'slug_id': file_meta.get('id'), # Keep both for safety
                    'href': f"https://bunkr.cr/f/{urllib.parse.quote(str(slug))}" if slug else None,
                    'title': display_name,
                    'original': display_name,
                    'size': self._safe_int(file_meta.get('size', 0)),
                    # Unpack remaining keys to ensure no drift in metadata capture
                    **{k: v for k, v in file_meta.items() if k not in ['id', 'name', 'slug', 'original', 'size']}
                })
                
        return parsed_files

    async def scrape_album(self, session, url, search_term, album_number_index=0, save_json=False):
        """
        Performs deep resolution using 'advanced=1' and syncs to DB.
        """
        # Inject 'advanced=1' parameter to force the JS array to load in the HTML
        parsed_url = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed_url.query))
        params['advanced'] = '1'
        optimized_url = urllib.parse.urlunparse(
            parsed_url._replace(query=urllib.parse.urlencode(params))
        )
        
        res = await execute_request_with_retry_async(session, optimized_url, headers=HEADERS)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Capture the real album title from page metadata
        album_title = soup.title.string.replace(" - Bunkr", "") if soup.title else "Unknown Album"
        
        # RESTORED FEEDBACK MESSAGE
        console.print(f"\n[bold green][+][/bold green] Selected: #[bold yellow]{album_number_index}[/bold yellow] - [bold white]{album_title}[/bold white]")
        console.print(f"[bold yellow][*][/bold yellow] STEP 2: Deep resolving optimized album view...")

        album_size, total_files = self.parse_album_header_metadata(soup)
        final_files = self.extract_advanced_album_files(res.text)
        
        # Package for DB
        data = {
            'search_term': search_term or "N/A",
            'selected_album': {
                'title': album_title.strip(),
                'album_index_number': album_number_index,
                'aggregate_size': album_size if album_size else "0 MB",
                'clean_file_count': total_files if total_files else f"{len(final_files)} files"
            },
            'files_found': final_files
        }
        
        if save_json:
            clean_term = re.sub(r'[^\w\s-]', '', search_term).strip().lower().replace(' ', '_')
            out_filename = f"scraped_{clean_term}_{int(time.time())}.json"
            try:
                with open(out_filename, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[!] JSON Backup Failed: {e}")

        # Sync with Database via register_album_from_json
        return self.db.register_album_from_json(data)