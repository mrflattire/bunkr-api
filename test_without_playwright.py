import re
import asyncio
import json
import urllib.parse
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

def parse_albums_from_html(html):
    """Extract album information from search results page"""
    soup = BeautifulSoup(html, 'html.parser')
    albums = []
    
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        text = link.get_text(strip=True)
        
        if re.search(r'/a/[\w-]+', href) and text:
            albums.append({
                'title': text,
                'url': href if href.startswith('http') else 'https://bunkr.cr' + href
            })
    
    seen = set()
    unique_albums = []
    for album in albums:
        if album['url'] not in seen:
            unique_albums.append(album)
            seen.add(album['url'])
    
    return unique_albums

def parse_files_from_album(html):
    """Extract initial file information from album page using specific container targets"""
    soup = BeautifulSoup(html, 'html.parser')
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
            
            files.append({
                'slug_id': slug_id,
                'href': href if href.startswith('http') else 'https://bunkr.cr' + href,
                'title': title,
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
                    'true_file_id': None
                })
                
    return files

async def run_scraper():
    # Initialize an async session impersonating Chrome to handle TLS/JA3 profiles
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
                print(f"  {i}. {album['title'][:60]}")
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
            album_html = res.text
            
            # ====== STEP 3: PARSE INITIAL FILES FROM ALBUM ======
            print("\n[*] STEP 3: Parsing file items from album grid layout...")
            files = parse_files_from_album(album_html)
            print(f"[+] Found {len(files)} initial items in the album grid.")
            
            # ====== STEP 4: DEEP RESOLUTION OF TRUE FILE IDs ======
            print("\n[*] STEP 4: Resolving absolute database IDs from internal file links...")
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
                        # Fallback to any script tag possessing data-file-id
                        script_el = file_soup.find("script", attrs={"data-file-id": True})
                        if script_el:
                            file_item['true_file_id'] = script_el["data-file-id"]
                        else:
                            print(f"  [-] Could not resolve data-file-id for slug: {file_item['slug_id']}")
                            
                except Exception as sub_err:
                    print(f"  [-] Connection error reading page {file_item['href']}: {sub_err}")
                
                final_files.append(file_item)
                # Polite delay to prevent connection choking
                await asyncio.sleep(0.5)

            # Print quick terminal sample visualization
            print(f"\n[+] Deep resolution complete. Sample records:")
            for i, f_rec in enumerate(final_files[:20], 1):
                id_str = f" [True ID: {f_rec['true_file_id']}]" if f_rec['true_file_id'] else " [ID: Not Found]"
                print(f"  {i}. {f_rec['title']}{id_str}")
                print(f"     {f_rec['href']}")
            
            # Save comprehensive results mapping to file
            results = {
                'search_term': 'inpossibleoreo',
                'selected_album': selected_album,
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