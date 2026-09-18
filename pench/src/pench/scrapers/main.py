"""
CLI to run site scrapers.

Usage:
    uv run main.py                          # List available scrapers
    uv run main.py mypro                    # Run mypro once (visible browser)
    uv run main.py mypro --headless         # Run without browser UI
    uv run main.py mypro --loop             # Run mypro forever
    uv run main.py all                      # Run all scrapers once each
    uv run main.py all --loop               # Run all scrapers forever (production)
    uv run main.py all --loop --workers 3   # Run forever with 3 workers (default: 2)
"""

import asyncio
import importlib
import inspect
import logging
import random
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser

from pench.scrapers.sites.base import BaseScraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
)

# Graceful shutdown flag
shutdown_event: asyncio.Event | None = None


def discover_scrapers() -> dict[str, type[BaseScraper]]:
    """Auto-discover all scrapers from sites/ folder."""
    scrapers = {}
    sites_dir = Path(__file__).parent / "sites/working"

    for file in sites_dir.glob("site_*.py"):
        module_name = f"pench.scrapers.sites.working.{file.stem}"
        try:
            module = importlib.import_module(module_name)
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseScraper) and obj is not BaseScraper:
                    scrapers[obj.name] = obj
        except Exception as e:
            logging.warning(f"Failed to load {module_name}: {e}")

    return scrapers


SCRAPERS = discover_scrapers()


def list_scrapers():
    """Print available scrapers."""
    print("Available scrapers:")
    for name, cls in sorted(SCRAPERS.items()):
        print(f"  {name:15} {cls.url}")


async def run_one(name: str, loop: bool, headless: bool):
    """Run a single scraper."""
    scraper_class = SCRAPERS.get(name)
    if not scraper_class:
        print(f"Unknown scraper: {name}")
        list_scrapers()
        sys.exit(1)

    scraper = scraper_class(headless=headless)
    cycles = 0 if loop else 1
    await scraper.run_loop(cycles=cycles)


async def run_all(num_workers: int = 2, loop: bool = False, headless: bool = False):
    """
    Run all scrapers with a worker pool.

    Args:
        num_workers: Max concurrent browsers
        loop: If False, run each scraper once. If True, run forever.
        headless: Run browsers without UI
    """
    global shutdown_event
    shutdown_event = asyncio.Event()

    if not SCRAPERS:
        logging.warning("No scrapers found!")
        return

    queue: asyncio.Queue[type[BaseScraper]] = asyncio.Queue()

    # Fill queue with all scrapers in random order
    # (so multiple containers pick different starting points)
    scraper_list = list(SCRAPERS.values())
    random.shuffle(scraper_list)
    for scraper_class in scraper_list:
        await queue.put(scraper_class)

    mode = "forever" if loop else "once each"
    logging.info(
        f"Starting {num_workers} workers for {len(SCRAPERS)} scrapers ({mode})"
    )
    logging.info("Press Ctrl+C to shutdown gracefully")

    # Setup signal handlers for graceful shutdown
    def handle_shutdown(sig, frame):
        logging.info("\nReceived shutdown signal, finishing current work...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    async def worker(worker_id: int):
        """Worker that processes scrapers from the queue."""
        # Restart browser every N runs to prevent resource exhaustion
        BROWSER_RESTART_INTERVAL = 100

        async with async_playwright() as playwright:
            browser: Browser | None = None
            runs_since_restart = 0

            try:
                while not shutdown_event.is_set():
                    # Launch/restart browser if needed
                    if browser is None or runs_since_restart >= BROWSER_RESTART_INTERVAL:
                        if browser:
                            logging.info(f"[Worker {worker_id}] Restarting browser after {runs_since_restart} runs")
                            await browser.close()
                            browser = None  

                        try:
                            browser = await playwright.chromium.launch(headless=headless)
                            runs_since_restart = 0
                            logging.info(f"[Worker {worker_id}] Browser launched (headless={headless})")
                        except Exception as e:
                            logging.error(f"[Worker {worker_id}] Failed to launch browser: {e}")
                            await asyncio.sleep(5)
                            continue

                    # Get next scraper from queue
                    if loop:
                        # In loop mode, wait for items (workers > scrapers is fine)
                        try:
                            scraper_class = await asyncio.wait_for(queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            # Check shutdown flag periodically
                            continue
                    else:
                        # In non-loop mode, exit when queue is empty
                        try:
                            scraper_class = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    scraper = scraper_class(headless=headless)

                    try:
                        logging.info(f"[Worker {worker_id}] Running {scraper.name}")
                        await scraper.run_once(browser)
                        runs_since_restart += 1
                    except Exception as e:
                        logging.error(f"[Worker {worker_id}] {scraper.name} failed: {e}")
                        # If browser context creation failed, force browser restart
                        if "Browser.new_context" in str(e) or "Connection closed" in str(e):
                            logging.warning(f"[Worker {worker_id}] Browser connection issue detected, will restart browser")
                            if browser:
                                try:
                                    await browser.close()
                                except Exception:
                                    pass
                            browser = None
                            runs_since_restart = 0
                    finally:
                        queue.task_done()
                        # Re-queue for next cycle only if looping (and not shutting down)
                        if loop and not shutdown_event.is_set():
                            await queue.put(scraper_class)

                    # Jitter between runs
                    jitter = random.uniform(1.0, 3.0)
                    await asyncio.sleep(jitter)
            finally:
                # Close browser when worker exits
                if browser:
                    await browser.close()
                    logging.info(f"[Worker {worker_id}] Browser closed")

        logging.info(f"[Worker {worker_id}] Shutdown complete")

    # Start workers
    workers = [worker(i) for i in range(num_workers)]
    await asyncio.gather(*workers)
    logging.info("All workers stopped")


def main():
    args = sys.argv[1:]

    if not args:
        list_scrapers()
        return

    # Parse --workers N
    num_workers = 2  # default
    if "--workers" in args:
        idx = args.index("--workers")
        if idx + 1 < len(args):
            try:
                num_workers = int(args[idx + 1])
            except ValueError:
                print("Error: --workers requires a number")
                sys.exit(1)

    loop = "--loop" in args
    headless = "--headless" in args
    site = [a for a in args if not a.startswith("--") and not a.isdigit()][0]

    if site == "all":
        asyncio.run(run_all(num_workers, loop, headless))
    else:
        asyncio.run(run_one(site, loop, headless))


if __name__ == "__main__":
    main()
