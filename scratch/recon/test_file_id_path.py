import logging
import os
import re
import urllib.parse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Enable request debugging
logging.basicConfig(level=logging.DEBUG)
urllib3_logger = logging.getLogger('urllib3')
urllib3_logger.setLevel(logging.DEBUG)

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Wipe any active terminal proxy flags that break requests pipelines
for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(key, None)

# Your starting target URL
SLUG_URL = "https://bunkr.cr/f/fnCXw7gJ2Tcib"

HEADERS = {
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

def resolve_file_id_and_route_b():
    session = requests.Session()
    
    # Add retry strategy for connection issues
    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    print(f"[*] Step 0: Fetching target file page: {SLUG_URL}...")
    try:
        # Disable SSL verification and capture response details for debugging
        page_res = session.get(SLUG_URL, headers=HEADERS, verify=False, allow_redirects=True, timeout=10)
        print(f"    [DEBUG] Status Code: {page_res.status_code}")
        print(f"    [DEBUG] Response Headers: {dict(page_res.headers)}")
        page_res.raise_for_status()
        html_content = page_res.text
        print(f"    [+] Successfully fetched page ({len(html_content)} bytes)")
        
        # Look for download-btn elements or variable allocations holding the raw ID digits
        # Matches formats like data-id="59975730" or download/59975730 or id: "59975730"
        match = re.search(r'(?:data-id=|download\/|id:\s*")(\d+)"?', html_content)
        
        if not match:
            print("    [-] Failed to locate the internal file ID in the HTML source.")
            return
            
        file_id = match.group(1)
        print(f"    [+] Extracted Internal File ID: {file_id}\n")
        
    except Exception as e:
        print(f"    [-] Failed to parse /f/ file page: {e}")
        print(f"    [DEBUG] Exception type: {type(e).__name__}")
        print(f"    [DEBUG] Full error: {e!r}")
        return

    # --- Proceed directly into your verified Route B Loop ---
    print(f"[*] Step 1: Querying metadata API for file ID: {file_id}...")
    meta_url = "https://dl.bunkr.cr/api/_001_v2"
    payload = {"id": file_id}
    
    # Configure headers matching Route B same-origin requirements
    route_b_headers = HEADERS.copy()
    route_b_headers.update({
        "Content-Type": "application/json",
        "Origin": "https://dl.bunkr.cr",
        "Referer": f"https://dl.bunkr.cr/file/{file_id}"
    })
    
    try:
        meta_res = session.post(meta_url, json=payload, headers=route_b_headers, verify=False)
        meta_res.raise_for_status()
        meta_data = meta_res.json()
        
        cdn_host = meta_data["mediafiles"]
        storage_path = meta_data["path"]
        original_name = meta_data["original"]
        
        print(f"    [+] Target CDN Host: {cdn_host}")
        print(f"    [+] Storage Path:    {storage_path}")
        print(f"    [+] File Name:       {original_name}\n")
        
    except Exception as e:
        print(f"    [-] Metadata lookup failed: {e}")
        return

    print("[*] Step 2: Requesting dynamic validation token from sign server...")
    encoded_path = urllib.parse.quote(storage_path)
    sign_url = f"https://glb-apisign.cdn.cr/sign?path={encoded_path}"
    
    try:
        sign_res = session.get(sign_url, headers=route_b_headers, verify=False)
        sign_res.raise_for_status()
        sign_data = sign_res.json()
        
        token = sign_data["token"]
        ex = sign_data["ex"]
        
    except Exception as e:
        print(f"    [-] Token signature generation failed: {e}")
        return

    print("[*] Step 3: Stitching parameters together into the final payload url...")
    encoded_name = urllib.parse.quote(original_name)
    final_cdn_url = f"{cdn_host}{storage_path}?n={encoded_name}&token={token}&ex={ex}"
    
    print("-" * 80)
    print("FINAL CDN ASSET URL:")
    print(final_cdn_url)
    print("-" * 80)

if __name__ == "__main__":
    resolve_file_id_and_route_b()