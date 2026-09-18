# Proxy Pool Guide

The proxy pool system automatically provisions fresh IPv6 proxies from Linode VMs every 3 hours, health checks them, and distributes them to scrapers. This keeps our IPs rotating to avoid detection and bans from target sites.

## Files

```
src/pench/config/
├── provision_linode_ipv6_proxies.py      # Gets new IPs, configures VMs
├── proxy.py                               # Loads and distributes proxies to scrapers
└── proxy_pools/linode_proxy_pool_*.json  # Where proxies are stored

/home/opc/
├── provision_linode_proxies.sh           # Main script (cron runs this)
└── cron.log                               # Logs
```

## How it works

**Cron schedule** (runs every 3 hours: 00:00, 03:00, 06:00, ..., 21:00 IST):
```bash
CRON_TZ=Asia/Kolkata
0 */3 * * * /home/opc/provision_linode_proxies.sh >> /home/opc/cron.log 2>&1
```

**Orchestration script** (`/home/opc/provision_linode_proxies.sh`):
```bash
#!/bin/bash
export TZ=Asia/Kolkata

echo "===== START: $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
start_ts=$(date +%s)

/usr/bin/timeout 1800 /usr/bin/docker exec \
  -e PYTHONUNBUFFERED=1 \
  pench-scraper-2 \
  /bin/sh -lc 'uv run python src/pench/config/provision_linode_ipv6_proxies.py all'

rc=$?

if [ $rc -eq 0 ]; then
  /usr/bin/sleep 60
  /usr/bin/docker ps -a --format '{{.Names}}' | /usr/bin/grep '^pench-scraper-' | /usr/bin/xargs -r /usr/bin/docker restart
fi

end_ts=$(date +%s)
echo "===== END: $(date '+%Y-%m-%d %H:%M:%S %Z') | Duration: $((end_ts - start_ts))s | Exit: $rc ====="

exit $rc
```

**Flow:**
1. Cron triggers script every 3 hours
2. Script calls `provision_linode_ipv6_proxies.py` (30 min timeout)
   - Generates 10 new IPv6s per VM
   - SSHs into each VM to configure networking
   - Health checks each proxy
   - Saves working ones to `proxy_pools/linode_proxy_pool_{vm}.json`
3. If successful: wait 60s, restart all scraper containers
4. Scrapers reload proxies from JSON files via `proxy.py`

**VM config** (in `provision_linode_ipv6_proxies.py`):
```python
VM_CONFIGS = {
    "vm3": {
        "host": "172.236.187.244",
        "ipv6_primary": "2600:3c16::2000:79ff:feff:0aad",
        "ipv6_range": "2600:3c16:e002:1800::/56",
    },
}
```

## Commands

**Check current proxies:**
```bash
docker exec -it pench-scraper-2 sh -lc 'for f in /app/src/pench/config/proxy_pools/linode_proxy_pool_vm*.json; do echo "== $f =="; cat "$f"; echo; done'
```

**Watch cron logs:**
```bash
tail -f /home/opc/cron.log
```

**Manually trigger rotation:**
```bash
/home/opc/provision_linode_proxies.sh
```

**Test a proxy:**
```bash
curl -x "http://[2600:3c16:e002:1800::1]:3001" https://api.ipify.org
```

## Adding more VMs

To scale up, add new VMs to `VM_CONFIGS` in `provision_linode_ipv6_proxies.py`:

```python
"vm4": {
    "host": "YOUR_VM_IP",
    "ipv6_primary": "YOUR_PRIMARY_IPV6",
    "ipv6_range": "YOUR_IPV6_RANGE/56",
}
```

Deploy, then run `/home/opc/provision_linode_proxies.sh` to test. New proxies show up in `linode_proxy_pool_vm4.json`.

## Troubleshooting

**Provisioning failed** → check `/home/opc/cron.log`

**Proxies not working** → restart scrapers, they'll reload the JSON files

**Cron not running** → `crontab -l` to verify, `chmod +x /home/opc/provision_linode_proxies.sh` if needed
