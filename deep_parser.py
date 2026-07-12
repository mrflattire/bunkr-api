import re
import asyncio
import json
import urllib.parse
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

def parse_albums_from_html(html):
    """Extract album information and file counts from search results page"""
    soup = BeautifulSoup(html, 'html.parser')
    albums = []
    
    # Target the container cards instead of loose 'a' tags to accurately tie title to counts
    for card in soup.find_all('a', href=True):
        href = card.get('href', '')
        
        if re.search(r'/a/[\w-]+', href):
            # Extract Title from inside the card
            title_tag = card.find('h3')
            title = title_tag.get_text(strip=True) if title_tag else card.get_text(strip=True)
            
            # Find the file count text (e.g., "6 files")
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
                    'file_count': file_count
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
        # Parse out size (e.g., "15.20 GB") and clean file count (e.g., "27 Files")
        # Text typically looks like: "(15.20 GB) 27 Files"
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
            
            # Attempt to find inline file size if it is listed within structural nodes
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

async def run_scraper():
    async with AsyncSession(impersonate="chrome") as session:
        try:
            # ====== STEP 1: SEARCH AND PARSE ALBUMS ======
            print("\n[*] STEP 1: Loading search results for 'inpossibleoreo'...")
            search_url = "https://balbums.st/?search=inpossibleoreo&mode=broad&per=20&sort=latest"
            
            res = await session.get(search_url, timeout=30)
            res.raise_for_status()
            
            albums = parse_albums_from_html(res.text)
            
            print(f"\n[+] Found {len(albums)} albums:")
            for i, album in enumerate(albums[:10], 1):
                count_str = f" [{album['file_count']}]" if album['file_count'] else ""
                print(f"  {i}. {album['title'][:60]}{count_str}")
                print(f"     {album['url']}")
            
            if not albums:
                print("[-] No albums found!")
                return
            
            # Prompt user to pick an album
            loop = asyncio.get_event_loop()
            choice = await loop.run_in_executor(None, input, "\n[?] Enter album number to explore (1-10): ")
            
            try:
                choice_idx = int(choice) - 1
                if 0 <= choice_idx < len(albums):
                    selected_album = albums[choice_idx]
                else:
                    print("[-] Invalid choice")
                    return
            except ValueError:
                print("[-] Invalid input")
                return
            
            print(f"\n[+] Selected: {selected_album['title']}")
            
            # ====== STEP 2: NAVIGATE TO ALBUM ======
            print(f"\n[*] STEP 2: Navigating to album via HTTP: {selected_album['url']}...")
            res = await session.get(selected_album['url'], timeout=30)
            res.raise_for_status()
            album_soup = BeautifulSoup(res.text, 'html.parser')
            
            # ====== STEP 3: PARSE METADATA & INITIAL FILES FROM ALBUM ======
            print("\n[*] STEP 3: Parsing album metadata and file grid layout...")
            album_size, total_files = parse_album_metadata(album_soup)
            
            if album_size and total_files:
                print(f"[+] Album Info -> Aggregate Size: {album_size} | Count: {total_files}")
            
            files = parse_files_from_album(album_soup)
            print(f"[+] Found {len(files)} initial items in the album grid.")
            
            # ====== STEP 4: DEEP RESOLUTION OF TRUE FILE IDs & SIZES ======
            print("\n[*] STEP 4: Resolving absolute database IDs and sizes from internal file links...")
            final_files = []
            
            for index, file_item in enumerate(files, start=1):
                print(f"  [{index}/{len(files)}] Fetching file landing page for: {file_item['title'][:50]}...")
                
                try:
                    file_res = await session.get(file_item['href'], timeout=20)
                    file_res.raise_for_status()
                    file_soup = BeautifulSoup(file_res.text, 'html.parser')
                    
                    # Target the element with id="fileTracker"
                    tracker = file_soup.find(id="fileTracker")
                    if tracker and tracker.has_attr("data-file-id"):
                        file_item['true_file_id'] = tracker["data-file-id"]
                    else:
                        script_el = file_soup.find("script", attrs={"data-file-id": True})
                        if script_el:
                            file_item['true_file_id'] = script_el["data-file-id"]
                        else:
                            print(f"  [-] Could not resolve data-file-id for slug: {file_item['slug_id']}")
                    
                    # Extract file size from the individual file page fallback if not found in grid layout
                    if not file_item['size']:
                        size_match = file_soup.find(text=re.compile(r'\b\d+(?:\.\d+)?\s*(?:KB|MB|GB)\b', re.IGNORECASE))
                        if size_match:
                            file_item['size'] = size_match.strip()
                            
                except Exception as sub_err:
                    print(f"  [-] Connection error reading page {file_item['href']}: {sub_err}")
                
                final_files.append(file_item)
                await asyncio.sleep(0.5)

            # Print quick terminal sample visualization
            print(f"\n[+] Deep resolution complete. Sample records:")
            for i, f_rec in enumerate(final_files[:20], 1):
                id_str = f" [True ID: {f_rec['true_file_id']}]" if f_rec['true_file_id'] else " [ID: Not Found]"
                size_str = f" ({f_rec['size']})" if f_rec['size'] else ""
                print(f"  {i}. {f_rec['title']}{size_str}{id_str}")
                print(f"     {f_rec['href']}")
            
            # Save comprehensive results mapping to file
            results = {
                'search_term': 'inpossibleoreo',
                'selected_album': {
                    **selected_album,
                    'aggregate_size': album_size,
                    'clean_file_count': total_files
                },
                'files_found': final_files
            }
            
            with open("album_files.json", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            print(f"\n[+] Enriched results saved out to album_files.json")
            
        except Exception as e:
            print(f"[-] Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_scraper())