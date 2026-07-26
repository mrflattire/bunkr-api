import asyncio
import re
import shutil
import subprocess

from playwright.async_api import async_playwright

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
            print("[+] Target Chrome instance is running.")
            return

    print("[*] Launching Chrome via PowerShell with remote debugging...")
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    ps_command = f'& "{CHROME_PATH}" --remote-debugging-port=9222 --user-data-dir="{PROFILE_DIR}"'
    subprocess.Popen([pwsh, "-NoProfile", "-Command", ps_command], creationflags=subprocess.CREATE_NEW_CONSOLE)

async def prompt_user(text: str) -> str:
    """Non-blocking terminal input helper for async loops."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, input, text)

async def run_scraper():
    ensure_chrome_is_running()
    await asyncio.sleep(1)

    async with async_playwright() as p:
        print(f"[*] Connecting over CDP to {CDP_URL}...")
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"[-] Connection failed: {e}")
            return
        
        context = browser.contexts[0]
        
        # Handle systemic pages restriction
        if context.pages:
            primary_page = context.pages[0]
            page = await context.new_page() if primary_page.url.startswith("chrome://") else primary_page
        else:
            page = await context.new_page()

        await page.route("**/*", intercept_route)
        
        try:
            # ----------------------------------------------------
            # STEP 1: Search and Parse Albums
            # ----------------------------------------------------
            print("\n[*] Step 1: Navigating to balbums.st search...")
            if "search=" not in page.url:
                await page.goto(
                    "https://balbums.st/?search=inpossibleoreo&mode=broad&per=20&sort=latest", 
                    wait_until="domcontentloaded",
                    timeout=30000
                )
            
            await page.wait_for_selector("a[href*='/album/']", timeout=10000)
            album_locators = await page.locator("a[href*='/album/']").all()
            
            albums = []
            for loc in album_locators:
                href = await loc.get_attribute("href")
                title = await loc.text_content()
                if href and title and title.strip():
                    # Resolve domain mapping dynamically if it's relative
                    full_url = href if href.startswith("http") else f"https://bunkr.cr{href}" if "bunkr" in href else f"https://balbums.st{href}"
                    albums.append({"title": title.strip(), "url": full_url})

            if not albums:
                print("[-] No albums found on search page layout.")
                return

            # ----------------------------------------------------
            # STEP 2: Interactive Prompt Selection
            # ----------------------------------------------------
            print("\n--- Discovered Albums ---")
            for idx, album in enumerate(albums):
                print(f"[{idx}] {album['title']}")
                
            selection = await prompt_user("\nSelect an album index to target: ")
            try:
                selected_idx = int(selection.strip())
                target_album = albums[selected_idx]
                print(f"\n[+] Selected: {target_album['title']}\nURL: {target_album['url']}")
            except (ValueError, IndexError):
                print("[-] Invalid entry selection. Exiting.")
                return

            # ----------------------------------------------------
            # STEP 3: Navigate to Selected Album & Sniff for File IDs
            # ----------------------------------------------------
            print("\n[*] Step 3: Navigating to chosen album layout...")
            await page.goto(target_album["url"], wait_until="domcontentloaded", timeout=30000)
            
            # Find all item links pointing to specific file views
            await page.wait_for_selector("a[href*='/f/']", timeout=10000)
            file_locators = await page.locator("a[href*='/f/']").all()
            
            print("\n--- Discovered Files and resolved File IDs ---")
            found_any = False
            for loc in file_locators:
                file_href = await loc.get_attribute("href")
                file_title = await loc.text_content()
                
                if file_href:
                    # Sniff out file token directly using standard string or slash boundaries
                    token_match = re.search(r'/f/([a-zA-Z0-9]+)', file_href)
                    if token_match:
                        file_id = token_match.group(1)
                        display_name = file_title.strip() if file_title else "Unnamed File"
                        print(f"[FOUND ID] {file_id} | Name: {display_name}")
                        found_any = True
            
            if not found_any:
                print("[-] No distinct file links/IDs could be scraped from this layout.")

        except Exception as e:
            print(f"[-] Operational Error: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())