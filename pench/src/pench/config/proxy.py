import os
import json
import random
import uuid
from pathlib import Path

PROXY_PORT_START = 3001

def get_proxy(sticky_session: bool = True) -> dict | None:
    """
    Get proxy configuration from environment variables or proxy pool files.
    Randomly selects from available proxy providers.

    Args:
        sticky_session: If True, adds a unique session ID to keep same IP
                       throughout the session (supports BrightData, Geonode, GoProxy, Oxylabs)

    Returns:
        Playwright proxy dict if any provider is configured, None otherwise
    """
    pool_providers = []       # Self-managed proxies (Linode IPv6, etc.)
    external_providers = []   # Paid proxy services (BrightData, Geonode, etc.)
    linode_token = os.getenv("LINODE_TOKEN", "")

    # Load proxy pool files 
    proxy_pools_dir = Path(__file__).parent / "proxy_pools"
    if proxy_pools_dir.exists():
        for pool_file in proxy_pools_dir.glob("*_proxy_pool_*.json"):
            try:
                pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
                ips = pool_data.get("ips", [])
                if not isinstance(ips, list) or not ips:
                    continue

                provider = pool_file.stem.replace("_proxy_pool_", "_")

                proxies = []
                for index, ip in enumerate(ips):
                    if not isinstance(ip, str) or not ip:
                        continue
                    proxies.append({
                        "host": ip,
                        "port": str(PROXY_PORT_START + index),
                        "username": "linode",
                        "password": linode_token,
                    })

                if proxies:
                    pool_providers.append({
                        "provider": provider,
                        "proxies": proxies,
                    })
            except (json.JSONDecodeError, OSError):
                continue

    # Load proxies from environment variables 
    proxy_count = int(os.getenv("PROXIES", "1"))
    for i in range(1, proxy_count + 1):
        host = os.getenv(f"PROXY_HOST{i}")
        if not host:
            continue

        port = os.getenv(f"PROXY_PORT{i}")
        if not port:
            continue

        username = os.getenv(f"PROXY_USERNAME{i}", "")
        password = os.getenv(f"PROXY_PASSWORD{i}", "")
        provider = os.getenv(f"PROXY_PROVIDER{i}", "Generic")

        external_providers.append({
            "provider": provider,
            "proxies": [{
                "host": host,
                "port": port,
                "username": username,
                "password": password,
            }],
        })

    # Select provider with 85% preference for pool-based proxies
    if pool_providers and external_providers:
        candidates = pool_providers if random.random() <= 1 else external_providers
    elif pool_providers:
        candidates = pool_providers
    elif external_providers:
        candidates = external_providers
    else:
        return None

    selected = random.choice(candidates)
    proxy = random.choice(selected["proxies"])
    host, port, username, password = proxy["host"], proxy["port"], proxy["username"], proxy["password"]
    provider = selected["provider"]
    is_pool_based = selected in pool_providers

    # Apply sticky-session logic only for external providers
    if sticky_session and username and not is_pool_based:
        session_id = uuid.uuid4().hex[:8]

        if provider == "BrightData":
            username = (username.replace("-country-", f"-session-{session_id}-country-")
                       if "-country-" in username else f"{username}-session-{session_id}")
        elif provider == "Geonode":
            port = str(random.randint(10000, 10900))
            username = f"{username}-session-{session_id}-lifetime-10"
        elif provider == "GoProxy":
            parts = username.split("-", 2) if "customer-" in username else []
            username = (f"{parts[0]}-{parts[1]}-{parts[2]}-session-{session_id}-time-30"
                       if parts else f"{username}-session-{session_id}-time-30")
        elif provider == "Oxylabs":
            port = str(random.randint(8001, 63000))
            username = username if username.startswith("user-") else f"user-{username}"
        elif provider == "Infatica":
            port = str(random.randint(10000, 10999))
        elif provider == "Proxyjet":
            username = f"{username}-ip-{session_id}"
        elif provider == "ProxyEmpire":
            username = f"{username}-{session_id}"

    # Build server URL (handle IPv6 addresses in brackets)
    server_url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"

    return {
        "server": server_url,
        "username": username,
        "password": password,
        "provider": provider,
    }
