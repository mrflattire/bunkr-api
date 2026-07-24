import cloudscraper
import re
import urllib.parse

FILE_SLUG = "fnCXw7gJ2Tcib"
SLUG_URL = f"https://bunkr.cr/f/{FILE_SLUG}"

print(f"[*] Step 0: Fetching target file page using cloudscraper: {SLUG_URL}...")

scraper = cloudscraper.create_scraper()

# Set up proper headers matching the working request from your browser
headers = {
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

# Add cookies if you have them
cookies = {
    "__PPU_puid": "16865481129874040575",
    "UGVyc2lzdFN0b3JhZ2U": "%7B%22CAIFRQ%22%3A%22ADuppwAAAAAAAAABADpRdQAAAAAAAAAB%22%2C%22CAIFRT%22%3A%22ADuppwAAAABqDpFQADpRdQAAAABqDpFQ%22%7D",
}

try:
    page_res = scraper.get(SLUG_URL, headers=headers, cookies=cookies, timeout=30)
    print(f"    [+] Status Code: {page_res.status_code}")
    
    if page_res.status_code == 200:
        html_content = page_res.text
        print(f"    [+] Successfully fetched page ({len(html_content)} bytes)\n")
        
        # Extract file ID
        match = re.search(r'(?:data-id=|download\/|id:\s*")(\d+)"?', html_content)
        if match:
            file_id = match.group(1)
            print(f"    [+] Extracted File ID: {file_id}\n")
            
            # Now try the metadata API call
            print(f"[*] Step 1: Querying metadata API for file ID: {file_id}...")
            meta_url = "https://dl.bunkr.cr/api/_001_v2"
            payload = {"id": file_id}
            
            meta_headers = {
                "Content-Type": "application/json",
                "Origin": "https://dl.bunkr.cr",
                "Referer": f"https://dl.bunkr.cr/file/{file_id}",
            }
            
            meta_res = scraper.post(meta_url, json=payload, headers=meta_headers, timeout=30)
            print(f"    [+] Metadata API Status: {meta_res.status_code}")
            
            if meta_res.status_code == 200:
                meta_data = meta_res.json()
                print(f"    [+] Metadata retrieved successfully:")
                print(f"        CDN Host: {meta_data.get('mediafiles')}")
                print(f"        Storage Path: {meta_data.get('path')}")
                print(f"        File Name: {meta_data.get('original')}")
            else:
                print(f"    [-] Metadata API failed: {meta_res.text[:500]}")
        else:
            print("    [-] Could not extract file ID from HTML")
            # Show a snippet for debugging
            print(f"    [DEBUG] HTML preview: {html_content[500:1000]}")
    else:
        print(f"    [-] Failed to fetch page. Status: {page_res.status_code}")
        print(f"    [-] Response: {page_res.text[:500]}")
            
except Exception as e:
    print(f"    [-] Error: {e}")
    import traceback
    traceback.print_exc()
