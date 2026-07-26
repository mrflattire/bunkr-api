import re
import subprocess

FILE_SLUG = "fnCXw7gJ2Tcib"
SLUG_URL = f"https://bunkr.cr/f/{FILE_SLUG}"

print(f"[*] Step 0: Fetching target file page using curl: {SLUG_URL}...")

# Use curl with proper TLS options
curl_cmd = [
    "curl",
    "-L",  # Follow redirects
    "-s",  # Silent mode
    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "-H", "Accept-Language: en-US,en;q=0.6",
    "-H", "Accept-Encoding: gzip, deflate, br",
    "--tlsv1.2",  # Force TLS 1.2
    SLUG_URL
]

try:
    result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode != 0:
        print(f"    [-] curl failed with return code {result.returncode}")
        print(f"    [-] stderr: {result.stderr}")
        print(f"    [-] stdout: {result.stdout[:500]}")
    else:
        html_content = result.stdout
        print(f"    [+] Successfully fetched page ({len(html_content)} bytes)")
        
        # Extract file ID
        match = re.search(r'(?:data-id=|download\/|id:\s*")(\d+)"?', html_content)
        if match:
            file_id = match.group(1)
            print(f"    [+] Extracted File ID: {file_id}\n")
        else:
            print("    [-] Could not extract file ID from HTML")
            # Show a snippet for debugging
            print(f"    [DEBUG] HTML preview: {html_content[500:1000]}")
            
except Exception as e:
    print(f"    [-] Error: {e}")
