import asyncio
import re
import shutil
import subprocess

from playwright.async_api import async_playwright

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE_DIR = r"C:\chrome-debug-profile"
CDP_URL = "http://localhost:9222"

# Aggressive fallback blocklist if items slip past your browser's adblocker extension
BLOCKED_PATTERNS = [
    r"wpadmngr\.com", r"salutetutortwiddling\.com", r"demandingoverdriveunthread\.com",
    r"nawpush\.com", r"capndr\.com", r"wpushsdk\.com", r"metricswpsh\.com"
]

async def intercept_route(route, request):
    url = request.url
    if any(re.search(pattern, url) for pattern in BLOCKED_PATTERNS):
        return await route.abort()
    
    # Block heavy media streams to keep execution fast
    if request.resource_type in ["media", "font"]:
        return await route.abort()

    await route.continue_()

def ensure_chrome_is_running():
    """Launches Chrome via modern PowerShell if port 9222 is unresponsive."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", 9222)) == 0:
            print("Target Chrome instance is already running.")
            return

    print("Launching Chrome via PowerShell with remote debugging enabled...")
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    ps_command = f'& "{CHROME_PATH}" --remote-debugging-port=9222 --user-data-dir="{PROFILE_DIR}"'
    
    subprocess.Popen(
        [pwsh, "-NoProfile", "-Command", ps_command],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

async def run_scraper():
    ensure_chrome_is_running()
    await asyncio.sleep(2)

    async with async_playwright() as p:
        print(f"Connecting Playwright over CDP to {CDP_URL}...")
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"Failed to connect to Chrome CDP: {e}")
            return
        
        context = browser.contexts[0]
        
        # Target the already open, active tab on your monitor instead of a blank background one
        if context.pages:
            page = context.pages[0]
            print(f"Attached to existing tab: {page.url}")
        else:
            page = await context.new_page()
            print("Opened a new tab.")
        
        # Route backup safety net
        await page.route("**/*", intercept_route)
        
        try:
            # Step 1: Navigate to balbums.st search if not already there
            print("\n[*] Step 1: Navigating to balbums.st search...")
            if "search=" not in page.url:
                await page.goto(
                    "https://balbums.st/?search=inpossibleoreo&mode=broad&per=20&sort=latest", 
                    wait_until="domcontentloaded",
                    timeout=30000
                )
            
            print("[+] Page loaded")
            await asyncio.sleep(2)  # Let it settle
            
            # Get cookies
            cookies_1 = await context.cookies()
            print(f"[+] Cookies after balbums search: {len(cookies_1)}")
            for c in cookies_1:
                print(f"    - {c['name']}: {c['value'][:40]}...")
            
            # Step 2: Navigate to bunkr album
            print("\n[*] Step 2: Navigating to bunkr.cr album...")
            await page.goto(
                "https://bunkr.cr/a/y87ymnDI",
                wait_until="domcontentloaded",
                timeout=30000
            )
            print("[+] Album page loaded")
            await asyncio.sleep(2)
            
            # Get cookies
            cookies_2 = await context.cookies()
            print(f"[+] Cookies after bunkr album: {len(cookies_2)}")
            for c in cookies_2:
                print(f"    - {c['name']}: {c['value'][:40]}...")
            
            # Step 3: Navigate to bunkr file
            print("\n[*] Step 3: Navigating to bunkr.cr file...")
            await page.goto(
                "https://bunkr.cr/f/fnCXw7gJ2Tcib",
                wait_until="domcontentloaded",
                timeout=30000
            )
            print("[+] File page loaded")
            await asyncio.sleep(2)
            
            # Get final cookies
            final_cookies = await context.cookies()
            print(f"\n[+] Final cookies: {len(final_cookies)}")
            for c in final_cookies:
                print(f"    - {c['name']}: {c['value']}")
            
            # Get page content
            content = await page.content()
            print(f"\n[+] Page content length: {len(content)} bytes")
            
            # Extract file ID
            import re
            match = re.search(r'(?:data-id=|download\/|id:\s*")(\d+)"?', content)
            if match:
                file_id = match.group(1)
                print(f"[+] Extracted File ID: {file_id}")
            else:
                print("[-] Could not find file ID")
                print(f"[DEBUG] Content preview: {content[500:1000]}")
            
            # Save results
            import json
            cookies_dict = {c['name']: c['value'] for c in final_cookies}
            with open("session_cookies.json", "w") as f:
                json.dump(cookies_dict, f, indent=2)
            print("\n[+] Cookies saved to session_cookies.json")
            
            with open("file_page.html", "w", encoding="utf-8") as f:
                f.write(content)
            print("[+] Page content saved to file_page.html")
            
        except Exception as e:
            print(f"[-] Error: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())