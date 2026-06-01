import asyncio
import time
import sys
from pathlib import Path

ROOT = Path("c:/Users/aman/reddit_stealth_scraper")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.youtube import YouTubeScraperService

success_count = 0
failure_count = 0
should_stop = False
lock = asyncio.Lock()

async def worker(worker_id: int, service: YouTubeScraperService, query: str, total_requests: int):
    global success_count, failure_count, should_stop
    
    for i in range(1, total_requests + 1):
        if should_stop:
            break
            
        req_start = time.time()
        try:
            results = await service.search(query, sort="relevance", timeframe="all", limit=5)
            elapsed = time.time() - req_start
            
            async with lock:
                success_count += 1
                current_total = success_count + failure_count
                print(
                    f"[Req {current_total:05d} | Worker {worker_id}] Status: OK | Results Count: {results.get('results_count')} | "
                    f"Time: {round(elapsed, 2)}s"
                )
        except Exception as e:
            elapsed = time.time() - req_start
            async with lock:
                failure_count += 1
                current_total = success_count + failure_count
                print(f"[Req {current_total:05d} | Worker {worker_id}] Request Failed/Blocked: {e} | Time: {round(elapsed, 2)}s")
                print(f"\n⚠️ Worker {worker_id} detected a block or error at request {current_total}! Halting stress test.")
                should_stop = True
                break

async def run_stress_test():
    print("=== Infinite YouTube Rate Limit Stress Test (Single Proxy) ===")
    service = YouTubeScraperService()
    
    # Force the service to use only the first proxy in our list
    if not service.proxies:
        print("No proxies configured! Stress testing direct connection.")
        proxy = None
    else:
        proxy = service.proxies[0]
        service.proxies = [proxy]
        print(f"Forcing service to use single proxy: {proxy}")
        
    start_time = time.time()
    query = "python programming tutorials"
    
    # Configure concurrency: 15 workers sending requests concurrently
    concurrency = 15
    requests_per_worker = 20000  # Practically infinite for our run
    print(f"\nLaunching {concurrency} concurrent workers. The test will run until YouTube blocks/rate-limits us...")
    
    workers = [
        worker(w_id, service, query, requests_per_worker)
        for w_id in range(1, concurrency + 1)
    ]
    
    await asyncio.gather(*workers)
                
    total_time = time.time() - start_time
    print("\n=== Test Summary ===")
    print(f"Total Requests Sent: {success_count + failure_count}")
    print(f"Successful Requests: {success_count}")
    print(f"Failed/Blocked Requests: {failure_count}")
    print(f"Total Time Elapsed: {round(total_time, 2)} seconds")
    if success_count + failure_count > 0:
        print(f"Average Speed: {round((success_count + failure_count) / total_time, 2)} requests per second")

if __name__ == "__main__":
    asyncio.run(run_stress_test())
