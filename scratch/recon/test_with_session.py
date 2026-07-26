import re

import cloudscraper

# Step 1: Visit the album page to establish session
ALBUM_URL = "https://bunkr.cr/a/y87ymnDI"
FILE_SLUG = "fnCXw7gJ2Tcib"
FILE_URL = f"https://bunkr.cr/f/{FILE_SLUG}"

print(f"[*] Step 0A: Visiting album page to establish session: {ALBUM_URL}...")

# Create scraper with persistent session
scraper = cloudscraper.create_scraper()

# Headers for album page request
album_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Sec-GPC": "1",
    "Accept-Language": "en-US,en;q=0.6",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Referer": "https://balbums.st/",
    "Accept-Encoding": "gzip, deflate, br",
}

try:
    album_res = scraper.get(ALBUM_URL, headers=album_headers, timeout=30)
    print(f"    [+] Album page Status: {album_res.status_code}")
    print(f"    [+] Cookies after album visit: {scraper.cookies}")
    
except Exception as e:
    print(f"    [-] Album visit failed: {e}")
    print("    [*] Continuing with manual cookies...")

# Step 2: Now visit the file page with established session
print(f"\n[*] Step 0B: Visiting file page using established session: {FILE_URL}...")

file_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Sec-GPC": "1",
    "Accept-Language": "en-US,en;q=0.6",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Referer": ALBUM_URL,  # Referer is the album page
    "Accept-Encoding": "gzip, deflate, br",
}

try:
    file_res = scraper.get(FILE_URL, headers=file_headers, timeout=30)
    print(f"    [+] Status Code: {file_res.status_code}")
    
    if file_res.status_code == 200:
        html_content = file_res.text
        print(f"    [+] Successfully fetched file page ({len(html_content)} bytes)\n")
        
        # Extract file ID
        match = re.search(r'(?:data-id=|download\/|id:\s*")(\d+)"?', html_content)
        if match:
            file_id = match.group(1)
            print(f"    [+] Extracted File ID: {file_id}\n")
        else:
            print("    [-] Could not extract file ID from HTML")
            print(f"    [DEBUG] HTML preview: {html_content[500:1000]}")
    else:
        print(f"    [-] Failed to fetch file page. Status: {file_res.status_code}")
        print(f"    [-] Response: {file_res.text[:500]}")
            
except Exception as e:
    print(f"    [-] File page visit failed: {e}")
    import traceback
    traceback.print_exc()
