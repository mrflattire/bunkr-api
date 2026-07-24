import re
import asyncio
import subprocess
import shutil
import json
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE_DIR = r"C:\chrome-debug-profile"
CDP_URL = "http://localhost:9222"

BLOCKED_PATTERNS = [
    r"wpadmngr\.com", r"salutetutortwiddling\.com", r"demandingoverdriveunthread\.com",
    r"nawpush\.com", r"capndr\.com", r"wpushsdk\.com", r"metricswpsh\.com"
]

async def intercept_route(route, request):
    url = request.url
    if any(re.search(pattern, url) for pattern in BLOCKED_PATTERNS):
        return await route.abort()
    if request.resource_type in ["media", "font"]:
        return await route.abort()
    await route.continue_()

def ensure_chrome_is_running():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", 9222)) == 0:
            print("[+] Chrome is already running.")
            return
    print("[*] Launching Chrome with remote debugging...")
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    ps_command = f'& "{CHROME_PATH}" --remote-debugging-port=9222 --user-data-dir="{PROFILE_DIR}"'
    subprocess.Popen([pwsh, "-NoProfile", "-Command", ps_command], creationflags=subprocess.CREATE_NEW_CONSOLE)

def parse_albums_from_html(html):
    """Extract album information from search results page"""
    soup = BeautifulSoup(html, 'html.parser')
    albums = []
    
    # Look for links that contain /a/ or /album/
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        text = link.get_text(strip=True)
        
        # Match album links
        if re.search(r'/a/[\w-]+', href) and text:
            albums.append({
                'title': text,
                'url': href if href.startswith('http') else 'https://bunkr.cr' + href
            })
    
    # Remove duplicates while preserving order
    seen = set()
    unique_albums = []
    for album in albums:
        if album['url'] not in seen:
            unique_albums.append(album)
            seen.add(album['url'])
    
    return unique_albums

def parse_files_from_album(html):
    """Extract file information from album page using specific container targets"""
    soup = BeautifulSoup(html, 'html.parser')
    files = []
    
    # Target the layout structure using 'theItem' wrappers
    items = soup.find_all('div', class_='theItem')
    
    for item in items:
        # Find the download hyperlink inside the item card
        link_tag = item.find('a', href=True, attrs={'aria-label': 'download'})
        if not link_tag:
            # Fallback: look for any link containing /f/ within the item container
            link_tag = item.find('a', href=re.compile(r'/f/'))
            
        if link_tag:
            href = link_tag.get('href', '')
            
            # Extract ID cleanly from the href
            id_match = re.search(r'/f/([\w\d]+)', href)
            file_id = id_match.group(1) if id_match else None
            
            # Find the filename text inside the paragraph helper tag
            name_tag = item.find('p', class_='theName')
            title = name_tag.get_text(strip=True) if name_tag else "Unknown Title"
            
            files.append({
                'id': file_id,
                'href': href if href.startswith('http') else 'https://bunkr.cr' + href,
                'title': title
            })
            
    # Global fallback to general href parsing across the page if specific structure isn't matched
    if not files:
        for link in soup.find_all('a', href=re.compile(r'/f/')):
            href = link.get('href', '')
            id_match = re.search(r'/f/([\w\d]+)', href)
            if id_match:
                files.append({
                    'id': id_match.group(1),
                    'href': href if href.startswith('http') else 'https://bunkr.cr' + href,
                    'title': link.get_text(strip=True) or "Link File"
                })
                
    return files

async def run_scraper():
    ensure_chrome_is_running()
    await asyncio.sleep(2)

    async with async_playwright() as p:
        print(f"\n[*] Connecting to Chrome CDP at {CDP_URL}...")
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"[-] Connection failed: {e}")
            return
        
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        await page.route("**/*", intercept_route)
        
        try:
            # ====== STEP 1: SEARCH AND PARSE ALBUMS ======
            print("\n[*] STEP 1: Loading search results for 'inpossibleoreo'...")
            await page.goto(
                "https://balbums.st/?search=inpossibleoreo&mode=broad&per=20&sort=latest",
                wait_until="domcontentloaded",
                timeout=30000
            )
            print("[+] Search page loaded")
            await asyncio.sleep(2)
            
            # Parse HTML directly without waiting for specific selectors
            search_html = await page.content()
            albums = parse_albums_from_html(search_html)
            
            print(f"\n[+] Found {len(albums)} albums:")
            for i, album in enumerate(albums[:10], 1):  # Show first 10
                print(f"  {i}. {album['title'][:60]}")
                print(f"     {album['url']}")
            
            if not albums:
                print("[-] No albums found! HTML preview:")
                print(search_html[500:1500])
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
            print(f"\n[*] STEP 2: Navigating to album: {selected_album['url']}...")
            await page.goto(selected_album['url'], wait_until="domcontentloaded", timeout=30000)
            print("[+] Album page loaded")
            await asyncio.sleep(2)
            
            # ====== STEP 3: PARSE FILES FROM ALBUM ======
            print("\n[*] STEP 3: Parsing files from album...")
            album_html = await page.content()
            files = parse_files_from_album(album_html)
            
            print(f"\n[+] Found {len(files)} files/items:")
            for i, file in enumerate(files[:20], 1):  # Show first 20
                id_str = f" [ID: {file['id']}]" if file['id'] else ""
                print(f"  {i}. {file['title']}{id_str}")
                if file['href']:
                    print(f"     {file['href']}")
            
            # Save results
            results = {
                'search_term': 'inpossibleoreo',
                'selected_album': selected_album,
                'files_found': files[:50]  # Save first 50
            }
            
            with open("album_files.json", "w") as f:
                json.dump(results, f, indent=2)
            print(f"\n[+] Results saved to album_files.json")
            
            # Keep browser open for inspection
            print("\n[*] Browser remains open for 20 seconds...")
            await asyncio.sleep(20)
            
        except Exception as e:
            print(f"[-] Error: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())