import asyncio
import json
from datetime import datetime

from playwright.async_api import async_playwright


async def establish_session():
    """Uses browser to establish complete session chain and extract cookies"""
    
    # Setup logging file
    log_file = open("network_log.txt", "w")
    
    def log_request(request):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_file.write(f"\n[{timestamp}] REQUEST\n")
        log_file.write(f"  URL: {request.url}\n")
        log_file.write(f"  Method: {request.method}\n")
        log_file.write(f"  Headers: {dict(request.headers)}\n")
        log_file.flush()
        print(f"[NET] {request.method} {request.url}")
    
    def log_response(response):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_file.write(f"\n[{timestamp}] RESPONSE\n")
        log_file.write(f"  URL: {response.url}\n")
        log_file.write(f"  Status: {response.status}\n")
        log_file.write(f"  Headers: {dict(response.headers)}\n")
        log_file.flush()
        print(f"[NET] {response.status} {response.url}")
    
    async with async_playwright() as p:
        print("[*] Launching browser (headless=False for visibility)...\n")
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        # Hook into network events
        page.on("request", log_request)
        page.on("response", log_response)
        
        try:
            # Step 1: Visit balbums.st home
            print("[*] Step 1: Visiting balbums.st...")
            log_file.write("\n========== STEP 1: balbums.st HOME ==========\n")
            await page.goto("https://balbums.st/", wait_until="networkidle", timeout=60000)
            print("[+] Step 1 Complete\n")
            
            # Step 2: Search on balbums.st
            print("[*] Step 2: Searching for album...")
            log_file.write("\n========== STEP 2: balbums.st SEARCH ==========\n")
            await page.goto("https://balbums.st/?search=inpossibleoreo&mode=broad&per=20&sort=latest", wait_until="networkidle", timeout=60000)
            print("[+] Step 2 Complete\n")
            
            # Get cookies after balbums.st
            cookies_after_balbums = await context.cookies()
            print(f"[+] Cookies after balbums.st: {len(cookies_after_balbums)}")
            for cookie in cookies_after_balbums:
                print(f"    - {cookie['name']}: {cookie['value'][:50]}...")
            print()
            
            # Step 3: Visit album on bunkr.cr
            print("[*] Step 3: Visiting album on bunkr.cr...")
            log_file.write("\n========== STEP 3: bunkr.cr ALBUM ==========\n")
            await page.goto("https://bunkr.cr/a/y87ymnDI", wait_until="networkidle", timeout=60000)
            print("[+] Step 3 Complete\n")
            
            # Get cookies after bunkr album
            cookies_after_album = await context.cookies()
            print(f"[+] Cookies after bunkr album: {len(cookies_after_album)}")
            for cookie in cookies_after_album:
                print(f"    - {cookie['name']}: {cookie['value'][:50]}...")
            print()
            
            # Step 4: Visit file on bunkr.cr
            print("[*] Step 4: Visiting file on bunkr.cr...")
            log_file.write("\n========== STEP 4: bunkr.cr FILE ==========\n")
            await page.goto("https://bunkr.cr/f/fnCXw7gJ2Tcib", wait_until="networkidle", timeout=60000)
            print("[+] Step 4 Complete\n")
            
            # Get final cookies
            final_cookies = await context.cookies()
            print(f"[+] Final cookies: {len(final_cookies)}")
            for cookie in final_cookies:
                print(f"    - {cookie['name']}: {cookie['value']}")
            print()
            
            # Extract page content to find file ID
            content = await page.content()
            
            # Save cookies to file for later use
            cookies_dict = {cookie['name']: cookie['value'] for cookie in final_cookies}
            with open("session_cookies.json", "w") as f:
                json.dump(cookies_dict, f, indent=2)
            print("[+] Cookies saved to session_cookies.json")
            
            # Save page content to file
            with open("file_page.html", "w", encoding="utf-8") as f:
                f.write(content)
            print("[+] Page content saved to file_page.html")
            
            # Try to extract file ID
            import re
            match = re.search(r'(?:data-id=|download\/|id:\s*")(\d+)"?', content)
            if match:
                file_id = match.group(1)
                print(f"\n[+] Extracted File ID: {file_id}")
            else:
                print("\n[-] Could not extract file ID")
                print(f"[DEBUG] Page preview: {content[1000:1500]}")
            
            print("\n[*] Keeping browser open for 10 seconds (you can inspect)...")
            await asyncio.sleep(10)
            
        except Exception as e:
            print(f"[-] Error: {e}")
            import traceback
            traceback.print_exc()
            log_file.write(f"\nERROR: {e}\n")
            log_file.write(traceback.format_exc())
        
        finally:
            log_file.close()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(establish_session())
