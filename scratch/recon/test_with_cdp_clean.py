import re
import asyncio
import subprocess
import shutil
import json
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
            print("Chrome instance is already running.")
            return
    print("Launching Chrome with remote debugging...")
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    ps_command = f'& "{CHROME_PATH}" --remote-debugging-port=9222 --user-data-dir="{PROFILE_DIR}"'
    subprocess.Popen(
        [pwsh, "-NoProfile", "-Command", ps_command],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

def get_cookies_from_js(cookies_str):
    """Parse document.cookie string into dict"""
    cookies_dict = {}
    if cookies_str:
        for pair in cookies_str.split("; "):
            if "=" in pair:
                key, val = pair.split("=", 1)
                cookies_dict[key] = val
    return cookies_dict

async def run_scraper():
    ensure_chrome_is_running()
    await asyncio.sleep(2)

    async with async_playwright() as p:
        print(f"Connecting to Chrome CDP at {CDP_URL}...")
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"Failed to connect: {e}")
            return
        
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        await page.route("**/*", intercept_route)
        
        all_cookies = {}
        
        try:
            # Step 1: balbums.st search
            print("\n[*] Step 1: balbums.st search")
            await page.goto(
                "https://balbums.st/?search=inpossibleoreo&mode=broad&per=20&sort=latest",
                wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(1)
            
            ctx_cookies = await context.cookies()
            js_cookies = await page.evaluate("() => document.cookie")
            parsed_js = get_cookies_from_js(js_cookies)
            
            print(f"    Context cookies: {len(ctx_cookies)}")
            print(f"    JavaScript cookies: {parsed_js}")
            all_cookies.update(parsed_js)
            
            # Step 2: bunkr.cr album
            print("\n[*] Step 2: bunkr.cr album")
            await page.goto(
                "https://bunkr.cr/a/y87ymnDI",
                wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(1)
            
            ctx_cookies = await context.cookies()
            js_cookies = await page.evaluate("() => document.cookie")
            parsed_js = get_cookies_from_js(js_cookies)
            
            print(f"    Context cookies: {len(ctx_cookies)}")
            print(f"    JavaScript cookies: {parsed_js}")
            all_cookies.update(parsed_js)
            
            # Step 3: bunkr.cr file
            print("\n[*] Step 3: bunkr.cr file")
            await page.goto(
                "https://bunkr.cr/f/fnCXw7gJ2Tcib",
                wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(1)
            
            ctx_cookies = await context.cookies()
            js_cookies = await page.evaluate("() => document.cookie")
            parsed_js = get_cookies_from_js(js_cookies)
            
            print(f"    Context cookies: {len(ctx_cookies)}")
            print(f"    JavaScript cookies: {parsed_js}")
            all_cookies.update(parsed_js)
            
            # Extract page content
            content = await page.content()
            print(f"\n[+] Page loaded: {len(content)} bytes")
            
            # Extract file ID with multiple patterns
            patterns = [
                r'data-id="(\d+)"',
                r'"id"\s*:\s*"(\d+)"',
                r'fileId["\']?\s*:\s*["\']?(\d+)',
                r'(\d+)',  # fallback: any number
            ]
            
            file_id = None
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    file_id = match.group(1)
                    print(f"[+] Found File ID: {file_id}")
                    break
            
            if not file_id:
                print("[-] Could not extract file ID")
                print(f"[DEBUG] HTML sample: {content[800:1200]}")
            
            # Save results
            print("\n[*] Saving results...")
            with open("session_cookies.json", "w") as f:
                json.dump(all_cookies, f, indent=2)
            print("[+] Cookies saved to session_cookies.json")
            
            with open("file_page.html", "w", encoding="utf-8") as f:
                f.write(content)
            print("[+] Page saved to file_page.html")
            
            print("\n[+] SUCCESS - Browser is still open for inspection")
            await asyncio.sleep(30)  # Keep open for 30 seconds
            
        except Exception as e:
            print(f"[-] Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())
