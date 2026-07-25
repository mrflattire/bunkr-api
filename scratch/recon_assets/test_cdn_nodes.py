import re
import requests

# Your collected list of active Bunkr CDN nodes
CDN_NODES = [
    "c1fr-b.cdn.cr",
    "c2rm-b.cdn.cr",
    "c4ta-b.cdn.cr",
    "c1sp-b.cdn.cr",
    "c3mb-b.cdn.cr",
    "c4s9-b.cdn.cr",
    "c1mp-b.cdn.cr",
    "c4s5-b.cdn.cr",
    "c2ck-b.cdn.cr",
    "c3vi1-b.cdn.cr",
    "c1be-b.cdn.cr",
    "c3pz-b.cdn.cr",
    "c2ch-b.cdn.cr",
    "prxp-b.cdn.cr"
]

# The target URL containing the asset path and fresh token parameters
faulty_url = "https://prxp-b.cdn.cr/storage/media/VTS_01_5-DUzavUbJ.VOB?n=VTS_01_5.VOB&token=12d6f221ee281f73697cbdd009a0443bb973af8e&ex=1784410577"

def find_working_cdn(target_url, nodes):
    # Extract the /storage/media/... path and query tokens from the faulty URL string
    match = re.match(r"https://[^/]+(.*?)$", target_url)
    if not match:
        print("[-] Error: The original URL structure is invalid.")
        return None
    
    path_and_tokens = match.group(1)
    
    # Emulate basic browser environment headers for the Angie proxies
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://bunkr.ru",
        "Accept": "*/*"
    }
    
    print(f"[*] Sweeping {len(nodes)} nodes for the asset...\n")
    
    for node in nodes:
        # Construct the new URL by swapping in the alternative node host
        test_url = f"https://{node}{path_and_tokens}"
        print(f"[-] Probing node: {node:<15} -> ", end="", flush=True)
        
        try:
            # HEAD request to safely inspect HTTP status code and content size without data download
            response = requests.head(test_url, headers=headers, allow_redirects=True, timeout=6)
            
            if response.status_code == 200:
                print("SUCCESS (200 OK)")
                
                # Format file size for easy readability if available
                bytes_size = response.headers.get("Content-Length")
                if bytes_size and bytes_size.isdigit():
                    mb_size = round(int(bytes_size) / (1024 * 1024), 2)
                    size_str = f"{mb_size} MB"
                else:
                    size_str = "unknown size"
                
                print(f"\n[=== LIVE STREAM LINK FOUND ===]")
                print(f"Operational Node: {node}")
                print(f"Asset File Size:  {size_str}")
                print(f"Direct Link:      {test_url}\n")
                return test_url
                
            elif response.status_code == 403:
                # Catch if the node returns a body structure check or basic block
                print("DENIED (403 Forbidden)")
            else:
                print(f"FAILED (Status: {response.status_code})")
                
        except requests.exceptions.Timeout:
            print("FAILED (Network Timeout)")
        except requests.exceptions.RequestException:
            print("FAILED (Connection Error/Refused)")
            
    print("\n[-] Critical: None of the alternative nodes in this batch accepted the token path.")
    return None

# Execute the sweeping rotation matrix
working_link = find_working_cdn(faulty_url, CDN_NODES)
