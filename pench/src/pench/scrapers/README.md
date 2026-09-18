# Pench - UPI Scraper

A framework for scraping UPI IDs from scam websites.

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Get an .env file with all creds, place it in root.

# 3. List available scrapers
uv run scrape

# 4. Run a scraper
uv run scrape mypro
```

## Adding Dependencies

If you need a new Python package:

```bash
uv add requests
uv sync
```

Dependencies are tracked in `pyproject.toml`. Don't edit it manually—use `uv add`.

## Project Structure

```
pench/
├── sites/                  # 👈 Where you write scrapers
│   ├── base.py             # Base class (don't edit)
│   ├── example.py          # 👈 Copy this for new sites
│   ├── working/            # ✅ Auto-detected scrapers (production-ready)
│   │   ├── site_mypro.py
│   │   ├── site_tiranga.py
│   │   └── ...
│   └── not_working/        # ❌ Broken/WIP scrapers (not auto-detected)
│       ├── site_jie.py
│       └── ...
├── db/
│   ├── dto.py              # Data model for UPI records
│   └── client.py           # Supabase client
├── services/
│   ├── extraction.py       # UPI extraction (HTML + QR)
│   ├── bucket.py           # Screenshot upload to GCP
│   └── slack.py            # Slack notifications for failures
├── main.py                 # CLI to run scrapers
└── artifacts/              # Temporary screenshots (auto-cleaned)
```

> ⚠️ **Important:** Only scrapers in `sites/working/` are auto-detected by the CLI. Files in `sites/not_working/` are ignored. Move scrapers between folders as they break or get fixed.

---

## Adding a New Site (Step by Step)

> ⚠️ **Important:** 
> - Site files must be named `site_*.py` (e.g., `site_mynewsite.py`) to be auto-discovered
> - They must be placed in `sites/working/` directory (files in `sites/not_working/` are ignored)
> - Only working, production-ready scrapers should be in `sites/working/`

### Step 1: Copy the template

```bash
cp sites/example.py sites/working/site_mynewsite.py
```

### Step 2: Update the class

Open `sites/working/site_mynewsite.py` and change:

```python
class MyNewSiteScraper(BaseScraper):
    name = "mynewsite"  # This is what you use in the CLI
    url = "https://mynewsite.com/register?ref=123"
```

### Step 3: Write the scraping logic

The `scrape()` method should:

1. Navigate to the registration page
2. Fill in the form and register
3. Navigate to the recharge/deposit page
4. Select an amount and payment method
5. Get to the page showing the UPI ID or QR code
6. Return that page

```python
async def scrape(self, page: Page, context: BrowserContext) -> Page:
    creds = generate_credentials()
    # creds["mobile"]   - Indian phone (starts with 6-9)
    # creds["email"]    - Random email
    # creds["password"] - Random password

    await page.goto(self.url)
    await page.wait_for_timeout(3000)

    # Fill form (use browser DevTools to find selectors)
    await page.locator("input[name='phone']").fill(creds["mobile"])
    await page.locator("input[name='password']").fill(creds["password"])
    await page.locator("button[type='submit']").click()

    # ... navigate to payment page ...

    return page  # Return the page with UPI/QR visible
```

### Step 4: Test it

```bash
uv run scrape mynewsite
```

The base class handles screenshots, UPI extraction, GCP upload, and Supabase storage.

---

## Finding Selectors (Important!)

Use Chrome DevTools to find the right selectors:

1. Open the site in Chrome
2. Right-click on an element → "Inspect"
3. Look for unique attributes

**Common selector patterns:**

```python
# By placeholder text
await page.locator("input[placeholder='Enter phone']").fill("9876543210")

# By name attribute
await page.locator("input[name='password']").fill("mypassword")

# By class
await page.locator(".submit-btn").click()

# By text content
await page.locator("text=Register").click()

# By role (accessibility)
await page.get_by_role("button", name="Submit").click()

# By test ID (if available)
await page.get_by_test_id("phone-input").fill("9876543210")
```

**Tip:** Use `page.wait_for_timeout(3000)` after navigation to let the page load.

---

## CLI Commands

```bash
# List all scrapers
uv run scrape

# Run a scraper once (visible browser for debugging)
uv run scrape mypro

# Run without browser UI (production)
uv run scrape mypro --headless

# Run a scraper forever
uv run scrape mypro --loop

# Run all scrapers once each (testing)
uv run scrape all

# Run all scrapers forever (production)
uv run scrape all --loop --headless

# Run all scrapers forever with 3 workers
uv run scrape all --loop --workers 3 --headless
```

| Flag          | Purpose                              |
| ------------- | ------------------------------------ |
| `--headless`  | Run without browser UI (for servers) |
| `--loop`      | Run forever instead of once          |
| `--workers N` | Max concurrent browsers (default: 2) |

| Command                                | Purpose                    |
| -------------------------------------- | -------------------------- |
| `uv run scrape`                        | List available scrapers    |
| `uv run scrape <site>`                 | Run one scraper once       |
| `uv run scrape <site> --loop`          | Run one scraper forever    |
| `uv run scrape all`                    | Run all scrapers once each |
| `uv run scrape all --loop`             | Run all scrapers forever   |
| `uv run scrape all --loop --workers N` | Run forever with N workers |

### How Workers Work

Workers pull scrapers from a shared queue. At most N browsers run simultaneously (default: 2):

```
Workers: 2, Scrapers: [A, B, C, D]

Time 0:  Worker 0 → A,  Worker 1 → B     (2 browsers)
Time 1:  Worker 0 → C,  Worker 1 → D     (2 browsers)
Time 2:  (stops if no --loop, or repeats if --loop)
```

**Memory usage:** ~300-500 MB per worker. With `--workers 3`, expect ~1-1.5 GB RAM.

---

## Browser Configuration

Some sites block requests from datacenters. Configure per-site:

```python
class StrictSiteScraper(BaseScraper):
    name = "strict"
    url = "https://strict-site.com"

    use_browserbase = True   # Use cloud browser
    use_proxy = True         # Route through proxy
```

| Site blocks...     | `use_browserbase` | `use_proxy` |
| ------------------ | ----------------- | ----------- |
| Nothing            | `False`           | `False`     |
| Datacenter IPs     | `True`            | `True`      |
| Headless detection | `True`            | `False`     |

---

## Handling Common Scenarios

### New Tab Opens

```python
try:
    async with context.expect_page(timeout=10000) as new_page_info:
        await page.locator("button.pay").click()
    new_page = await new_page_info.value
    await new_page.wait_for_load_state("networkidle")
    return new_page
except:
    # No new tab, continue on same page
    return page
```

### Random Selection (Payment Methods)

```python
options = page.locator(".payment-option")
count = await options.count()
if count > 0:
    index = random.randint(0, count - 1)
    await options.nth(index).click()
```

### Waiting for Elements

```python
# Wait for element to appear
await page.locator(".result").wait_for(state="visible", timeout=10000)

# Wait fixed time
await page.wait_for_timeout(3000)  # 3 seconds
```

---

## Environment Variables

Ask for a `.env` file with all creds.

---

## Database Schema

UPIs are saved to Supabase with these fields:

| Field              | Description                                 |
| ------------------ | ------------------------------------------- |
| `upi`              | The extracted UPI ID (e.g., `merchant@ybl`) |
| `screenshot_url`   | Link to screenshot proof                    |
| `raw_source_url`   | The site URL you scraped                    |
| `payment_page_url` | The specific page where UPI was found       |
| `source_type`      | Always `"WEBSITE"` for manual scrapers      |

---

## Troubleshooting

### "Element not found"

-    Add `await page.wait_for_timeout(3000)` before interacting
-    Check if the selector is correct using browser DevTools
-    The site might have changed its HTML structure

### "No UPI extracted"

-    The UPI might be in an image/QR code (extraction tries both)
-    Check `artifacts/` folder for the screenshot
-    The QR code might be too small or blurry

### "Connection refused"

-    Check your `.env` file has correct credentials
-    Make sure you ran `uv sync` to install dependencies

### Site blocks the scraper

-    Set `use_browserbase = True` and `use_proxy = True`
-    Some sites are very strict and may need manual intervention

---

## Known Limitations (Future Work)

The worker pool has some edge cases that aren't handled yet:

### 1. Lost Scraper on Worker Crash

If a worker itself crashes (not just a scraper failure), the scraper it was processing is lost from the queue until restart.

**Potential fix:** Wrap entire worker in try/except and re-queue scraper on crash.

### 2. No Error Backoff

If a site is down, the scraper fails → re-queued → fails → re-queued. This hammers the failing site.

**Potential fix:** Track failure count per scraper, add exponential backoff (e.g., 1s → 2s → 4s → 8s).

### 3. Memory Leak Over Time

Workers run forever. If there's any memory leak in Playwright/Chrome, it accumulates.

**Potential fix:** Restart workers after N cycles, or monitor memory and restart if threshold exceeded.

### 4. Uneven Scraper Distribution

Fast scrapers run more frequently than slow ones. If you need each scraper to run equally often, this design doesn't guarantee that.

**Potential fix:** Use separate queues per scraper, or track last-run time and prioritize overdue scrapers.

### 5. Data Centre Proxy Rotation

Currently using data centre proxy rotation for high speed, but this can cause issues like scammers getting to know that an automated process is happening. For now, we're moving forward with this approach since it's fast and cheap.

**Potential fix:** Switch to residential proxies or implement more sophisticated fingerprinting/stealth techniques to better mimic human behavior.
