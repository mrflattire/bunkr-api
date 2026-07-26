import re
import time
import requests

# List of target Bunkr CDN nodes
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

target_url = "https://prxp-b.cdn.cr/storage/media/VTS_01_5-DUzavUbJ.VOB?n=VTS_01_5.VOB&token=12d6f221ee281f73697cbdd009a0443bb973af8e&ex=1784410577"

def benchmark_cdn_nodes(original_url, nodes):
    # Extract path and query parameters
    match = re.match(r"https://[^/]+(.*?)$", original_url)
    if not match:
        print("[-] Error: Invalid target URL structure.")
        return
    
    path_and_tokens = match.group(1)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://bunkr.ru",
        "Accept": "*/*",
        # Request only the first byte to verify server speed and payload throughput safely
        "Range": "bytes=0-0"
    }

    results = []
    print(f"[*] Sweeping & Benchmarking {len(nodes)} CDN nodes...\n")

    for node in nodes:
        test_url = f"https://{node}{path_and_tokens}"
        print(f"[-] Testing {node:<15} -> ", end="", flush=True)

        try:
            start_time = time.perf_counter()
            
            # Using GET with stream=True and Range header to test initial node response time
            response = requests.get(test_url, headers=headers, stream=True, timeout=5, allow_redirects=True)
            
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            # 200 OK or 206 Partial Content indicates a successful payload hit
            if response.status_code in (200, 206):
                print(f"ONLINE | Response Time: {elapsed_ms:.2f} ms")
                results.append({
                    "node": node,
                    "url": test_url,
                    "latency": elapsed_ms,
                    "status": response.status_code
                })
            elif response.status_code == 403:
                print("DENIED (403 Forbidden)")
            else:
                print(f"FAILED (Status {response.status_code})")

            response.close()

        except requests.exceptions.Timeout:
            print("TIMEOUT (>5000 ms)")
        except requests.exceptions.RequestException:
            print("CONNECTION ERROR")

    # Sort results by response speed (fastest latency first)
    results.sort(key=lambda x: x["latency"])

    print("\n" + "="*50)
    print("           RESPONSIVENESS RANKINGS           ")
    print("="*50)

    if not results:
        print("[-] No responsive or valid nodes found.")
        return

    for idx, item in enumerate(results, start=1):
        print(f"{idx:2d}. Node: {item['node']:<15} | Latency: {item['latency']:.2f} ms")
    
    fastest = results[0]
    print("\n[=== FASTEST OPERATIONAL CDN LINK ===]")
    print(f"Node:    {fastest['node']}")
    print(f"Latency: {fastest['latency']:.2f} ms")
    print(f"URL:     {fastest['url']}\n")

if __name__ == "__main__":
    benchmark_cdn_nodes(target_url, CDN_NODES)