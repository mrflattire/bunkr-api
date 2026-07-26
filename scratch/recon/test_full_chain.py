import re

import cloudscraper

print("[*] Starting complete session chain to access bunkr file...\n")

# Create single scraper with persistent session
scraper = cloudscraper.create_scraper()

# Step 1: Visit balbums.st home page
print("[*] Step 1: Visiting balbums.st home page...")
try:
    headers_1 = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Sec-GPC": "1",
        "Accept-Language": "en-US,en;q=0.5",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Accept-Encoding": "gzip, deflate, br",
    }
    
    res_1 = scraper.get("https://balbums.st/", headers=headers_1, timeout=30)
    print(f"    [+] Status: {res_1.status_code}")
    print(f"    [+] Cookies: {dict(scraper.cookies)}\n")
    
except Exception as e:
    print(f"    [-] Error: {e}\n")

# Step 2: Search on balbums.st
print("[*] Step 2: Searching for album on balbums.st...")
try:
    headers_2 = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Sec-GPC": "1",
        "Accept-Language": "en-US,en;q=0.8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Referer": "https://balbums.st/",
        "Accept-Encoding": "gzip, deflate, br",
    }
    
    search_url = "https://balbums.st/?search=inpossibleoreo&mode=broad&per=20&sort=latest"
    res_2 = scraper.get(search_url, headers=headers_2, timeout=30)
    print(f"    [+] Status: {res_2.status_code}")
    print(f"    [+] Cookies: {dict(scraper.cookies)}\n")
    
except Exception as e:
    print(f"    [-] Error: {e}\n")

# Step 3: Visit album on bunkr.cr
print("[*] Step 3: Visiting album page on bunkr.cr...")
try:
    headers_3 = {
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
    
    album_url = "https://bunkr.cr/a/y87ymnDI"
    res_3 = scraper.get(album_url, headers=headers_3, timeout=30)
    print(f"    [+] Status: {res_3.status_code}")
    print(f"    [+] Cookies: {dict(scraper.cookies)}\n")
    
except Exception as e:
    print(f"    [-] Error: {e}\n")

# Step 4: Visit file on bunkr.cr (final destination)
print("[*] Step 4: Visiting file page on bunkr.cr...")
try:
    headers_4 = {
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
        "Referer": "https://bunkr.cr/a/y87ymnDI",
        "Accept-Encoding": "gzip, deflate, br",
    }
    
    file_url = "https://bunkr.cr/f/fnCXw7gJ2Tcib"
    res_4 = scraper.get(file_url, headers=headers_4, timeout=30)
    print(f"    [+] Status: {res_4.status_code}")
    print(f"    [+] Cookies: {dict(scraper.cookies)}")
    
    if res_4.status_code == 200:
        html_content = res_4.text
        print(f"    [+] Successfully fetched file page ({len(html_content)} bytes)\n")
        
        # Extract file ID
        match = re.search(r'(?:data-id=|download\/|id:\s*")(\d+)"?', html_content)
        if match:
            file_id = match.group(1)
            print(f"    [+] Extracted File ID: {file_id}\n")
        else:
            print("    [-] Could not extract file ID from HTML")
    else:
        print(f"    [-] Failed! Status: {res_4.status_code}")
    
except Exception as e:
    print(f"    [-] Error: {e}")
    import traceback
    traceback.print_exc()
