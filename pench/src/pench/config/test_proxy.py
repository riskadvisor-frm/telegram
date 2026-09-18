"""
Test script to verify proxy and sticky sessions.

Usage:
    uv run test_proxy.py                    # Quick test with sticky session (3 requests)
    uv run test_proxy.py --sticky 10        # Test sticky session with 10 requests
    uv run test_proxy.py --no-sticky 5      # Test without sticky session (should get different IPs)
    uv run test_proxy.py --details          # Show detailed IP info on first request
"""

import argparse
import asyncio
import re

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from pench.config.proxy import get_proxy

load_dotenv()


async def get_ip(page) -> str | None:
    """Get IP address from icanhazip.com"""
    try:
        await page.goto("https://ipv4.icanhazip.com/", wait_until="networkidle", timeout=15000)
        ip_content = (await page.content()).strip()
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", ip_content)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"      ⚠️  Failed to get IP: {e}")
    return None


async def get_ip_details(page, ip: str):
    """Get detailed information about an IP address"""
    print(f"\n   📍 Fetching details for IP: {ip}...")
    try:
        response = await page.request.get(
            f"http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as,query",
            timeout=10000,
        )
        if response.ok:
            data = await response.json()
            if data.get("status") == "success":
                print("\n      📋 IP Details (ip-api.com):")
                print(f"         IP Address: {data.get('query', 'N/A')}")
                print(
                    f"         Location: {data.get('city', 'N/A')}, {data.get('regionName', 'N/A')}, {data.get('country', 'N/A')}"
                )
                print(f"         ISP: {data.get('isp', 'N/A')}")
                print(f"         Organization: {data.get('org', 'N/A')}")
                print(f"         AS Number: {data.get('as', 'N/A')}")
                print(f"         Timezone: {data.get('timezone', 'N/A')}")
                print(f"         Coordinates: {data.get('lat', 'N/A')}, {data.get('lon', 'N/A')}")

        response2 = await page.request.get(f"https://ipinfo.io/{ip}/json", timeout=10000)
        if response2.ok:
            data2 = await response2.json()
            print("\n      📋 Additional Details (ipinfo.io):")
            print(f"         Hostname: {data2.get('hostname', 'N/A')}")
            print(f"         City: {data2.get('city', 'N/A')}")
            print(f"         Region: {data2.get('region', 'N/A')}")
            print(f"         Country: {data2.get('country', 'N/A')}")
            print(f"         Org: {data2.get('org', 'N/A')}")
            print(f"         Postal: {data2.get('postal', 'N/A')}")
    except Exception as e:
        print(f"      ⚠️ Could not fetch IP details: {e}")


async def test_sticky_session(sticky: bool = True, num_requests: int = 3, show_details: bool = False):
    """
    Test sticky session by making multiple requests and checking if IP remains the same.

    Args:
        sticky: If True, use sticky session (should keep same IP)
               If False, creates fresh proxy config for each request (should get different IPs)
        num_requests: Number of requests to make
        show_details: If True, show detailed IP info on first request
    """
    print(f"🔒 Sticky Session: {'✅ Enabled' if sticky else '❌ Disabled'}")
    print(f"🔄 Making {num_requests} requests to check IP consistency...\n")

    ips = []
    async with async_playwright() as p:
        for i in range(num_requests):
            # For non-sticky: get fresh proxy config for each request (new session ID)
            # For sticky: get proxy once before loop and reuse
            if i == 0 or not sticky:
                proxy = get_proxy(sticky_session=sticky)

                if not proxy:
                    print("⚠️  PROXY_HOST not set - running without proxy")
                    return

                # Show proxy details on first request
                if i == 0:
                    session_marker = ""
                    username = proxy.get('username', '')

                    if '-session-' in username:
                        session_marker = f" [Session: {username.split('-session-')[1].split('-')[0]}]"
                    elif '-ip-' in username:
                        session_marker = f" [IP: {username.split('-ip-')[1].split('-')[0]}]"

                    print(f"   Proxy: {proxy['server']}")
                    print(f"   Provider: {proxy.get('provider', 'Unknown')}")
                    print(f"   First Username: {username}{session_marker}\n")

            # Create new browser for each request when not sticky
            # Reuse browser when sticky
            if i == 0 or not sticky:
                if i > 0 and not sticky:
                    await browser.close()
                browser = await p.chromium.launch(headless=True, proxy=proxy)
                page = await browser.new_page()

            print(f"   Request {i+1}/{num_requests}...", end=" ", flush=True)

            # Show session info for non-sticky requests
            if not sticky and i > 0:
                username = proxy.get('username', '')
                if '-session-' in username:
                    session_id = username.split('-session-')[1].split('-')[0]
                    print(f"[Session: {session_id}]", end=" ")
                elif '-ip-' in username:
                    session_id = username.split('-ip-')[1].split('-')[0]
                    print(f"[IP: {session_id}]", end=" ")

            ip = await get_ip(page)

            if ip:
                ips.append(ip)
                print(f"✅ {ip}")

                # Show details only on first request if requested
                if i == 0 and show_details:
                    await get_ip_details(page, ip)
            else:
                print("❌ Failed")

            # Small delay between requests
            if i < num_requests - 1:
                await asyncio.sleep(1)

        await browser.close()

    # Analyze results
    print(f"\n📊 Results:")
    print(f"   Total requests: {len(ips)}")
    print(f"   Unique IPs: {len(set(ips))}")

    if ips:
        unique_ips = list(set(ips))
        print(f"   IPs found: {', '.join(unique_ips)}")

        if len(unique_ips) == 1:
            print(f"\n   ✅ All requests used the same IP")
            if not sticky:
                print(f"      ⚠️  Note: Different session IDs but same IP - this may indicate:")
                print(f"         • Proxy provider reuses IPs from pool")
                print(f"         • Session IDs need more time to rotate")
                print(f"         • Provider-specific session format issue")
        else:
            print(f"\n   ✅ Multiple IPs detected:")
            for ip in unique_ips:
                count = ips.count(ip)
                print(f"      {ip}: {count} time(s)")
            if sticky:
                print(f"      ⚠️  Warning: Sticky session was enabled but IP changed!")

    print("\n✨ Done!")


def main():
    parser = argparse.ArgumentParser(description="Test proxy and sticky sessions")
    parser.add_argument(
        "--sticky",
        type=int,
        nargs="?",
        const=3,
        default=None,
        help="Test with sticky session enabled (optionally specify number of requests, default: 3)",
    )
    parser.add_argument(
        "--no-sticky",
        type=int,
        nargs="?",
        const=5,
        default=None,
        help="Test without sticky session (optionally specify number of requests, default: 5)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Show detailed IP information on first request",
    )

    args = parser.parse_args()

    # Default: sticky session with 3 requests
    if args.sticky is not None:
        asyncio.run(test_sticky_session(sticky=True, num_requests=args.sticky, show_details=args.details))
    elif args.no_sticky is not None:
        asyncio.run(test_sticky_session(sticky=False, num_requests=args.no_sticky, show_details=args.details))
    else:
        # Default behavior
        asyncio.run(test_sticky_session(sticky=True, num_requests=3, show_details=args.details))


if __name__ == "__main__":
    main()
