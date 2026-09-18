from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib import parse, request
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv


VM_SERVER_ID = "bfd4e33a-fb4c-41bc-bf08-7eeaac7bbfac"
# VM_SERVER_ID = "16230d95-f72d-464c-85c4-a4f91fe11d72"
TARGET_NETWORK_ID = "50ca4f27-ce1f-4d00-98f6-0b488e0a06b2"
SSH_HOST = "188.241.61.60"
# SSH_HOST = "45.64.107.131"
SSH_USER = "root"
SSH_KEY_PATH = str(Path("~/.ssh/sshvm.pem").expanduser())
SSH_PORT = 22
SSH_CONNECT_TIMEOUT_SECONDS = 10
SSH_COMMAND_TIMEOUT_SECONDS = 120

VM_INTERFACE = "enp1s0"
PRIMARY_IPV6 = "2405:7140:4::2ce"
PROXY_COUNT = 10
PROXY_START_PORT = 3001
MAX_ALLOCATION_ATTEMPTS = 40
VERIFY_TIMEOUT_SECONDS = 30
RETRY_DELAY_SECONDS = 2

STATE_DIR = Path(__file__).parent / "proxy_state"
STATE_FILE = STATE_DIR / "ipv6_proxy_state.json"
LOCK_FILE = STATE_DIR / "provision_ipv6_proxies.lock"

ROUTE_SCRIPT = "/usr/local/sbin/cloudpe-ipv6-routes.sh"
PROXY_SCRIPT = "/usr/local/bin/cloudpe-ipv6-http-proxy.py"
ROUTE_SERVICE = "cloudpe-ipv6-routes.service"
PROXY_SERVICE = "cloudpe-python-ipv6-proxy.service"
POLICY_TABLE_BASE = 20000

REMOTE_IP_TEST_HOST = "api6.ipify.org"
def normalize_ipv6(ip: str) -> str:
    normalized = str(ipaddress.ip_address(ip))
    if ipaddress.ip_address(normalized).version != 6:
        raise ValueError(f"Expected IPv6 address, got: {ip}")
    return normalized


def acquire_lock() -> object:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"Another run is already active: {LOCK_FILE}") from exc

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def load_state() -> dict[str, list[str]]:
    if not STATE_FILE.exists():
        return {"current_ips": [], "used_ips": []}

    raw_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state: dict[str, list[str]] = {}
    for key in ("current_ips", "used_ips"):
        values = raw_state.get(key, [])
        if not isinstance(values, list):
            raise RuntimeError(f"Invalid state file: {STATE_FILE}")

        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                ip = normalize_ipv6(value)
            except ValueError:
                continue
            if ip in seen:
                continue
            seen.add(ip)
            normalized.append(ip)
        state[key] = normalized
    return state


def save_state(current_ips: list[str], used_ips: list[str]) -> None:
    current_seen: set[str] = set()
    used_seen: set[str] = set()
    deduped_current: list[str] = []
    deduped_used: list[str] = []

    for ip in current_ips:
        if ip in current_seen:
            continue
        current_seen.add(ip)
        deduped_current.append(ip)

    for ip in used_ips:
        if ip in current_seen or ip in used_seen:
            continue
        used_seen.add(ip)
        deduped_used.append(ip)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "current_ips": deduped_current,
                "used_ips": deduped_used,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def api(token: str, network_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        f"{network_url}{path}",
        data=data,
        headers={"Content-Type": "application/json", "X-Auth-Token": token},
        method=method,
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API {method} {path} failed: {exc.code} {exc.reason} {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"API {method} {path} failed: {exc.reason}") from exc
    return json.loads(raw_body) if raw_body else {}


def authenticate() -> tuple[str, str]:
    auth_url = os.getenv("CLOUDPE_AUTH_URL", "https://in-west3.controlcloud.app:5000").rstrip("/")
    credential_id = os.getenv("CLOUDPE_APPLICATION_CREDENTIAL_ID", "").strip()
    credential_secret = os.getenv("CLOUDPE_APPLICATION_CREDENTIAL_SECRET", "").strip()
    if not credential_id:
        raise RuntimeError("Missing required environment variable: CLOUDPE_APPLICATION_CREDENTIAL_ID")
    if not credential_secret:
        raise RuntimeError("Missing required environment variable: CLOUDPE_APPLICATION_CREDENTIAL_SECRET")

    payload = {
        "auth": {
            "identity": {
                "methods": ["application_credential"],
                "application_credential": {
                    "id": credential_id,
                    "secret": credential_secret,
                },
            }
        }
    }
    req = request.Request(
        f"{auth_url}/v3/auth/tokens",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            response_headers = dict(response.headers.items())
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Authentication failed: {exc.code} {exc.reason} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Authentication failed: {exc.reason}") from exc

    token = response_headers.get("X-Subject-Token", "")
    if not token:
        raise RuntimeError("Authentication succeeded but X-Subject-Token header is missing")

    desired_interface = os.getenv("CLOUDPE_INTERFACE", "public").strip() or "public"
    desired_region = os.getenv("CLOUDPE_REGION", "").strip() or None
    endpoints = [
        endpoint
        for service in body.get("token", {}).get("catalog", [])
        if service.get("type") == "network"
        for endpoint in service.get("endpoints", [])
        if endpoint.get("interface") == desired_interface
    ]
    if not endpoints:
        raise RuntimeError(f"No {desired_interface} endpoint found for service type 'network'")

    network_url = next(
        (endpoint["url"] for endpoint in endpoints if endpoint.get("region") == desired_region),
        endpoints[0]["url"],
    )
    return token, network_url.rstrip("/")


def normalize_fixed_ips(port: dict) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in port.get("fixed_ips", []):
        subnet_id = item.get("subnet_id")
        ip_address = item.get("ip_address")
        if not subnet_id or not ip_address:
            continue
        normalized.append(
            {
                "subnet_id": str(subnet_id),
                "ip_address": str(ipaddress.ip_address(ip_address)),
            }
        )
    return normalized
def ssh_script(script: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [
                "ssh",
                "-i",
                SSH_KEY_PATH,
                "-p",
                str(SSH_PORT),
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
                "-o",
                "StrictHostKeyChecking=accept-new",
                f"{SSH_USER}@{SSH_HOST}",
                "bash -s",
            ],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            timeout=SSH_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.strip() if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
        detail = stderr or stdout or "remote command timed out"
        raise RuntimeError(
            f"SSH command timed out after {SSH_COMMAND_TIMEOUT_SECONDS}s: {detail}"
        ) from exc


def build_proxy_bindings(secondary_ipv6s: list[str]) -> list[dict[str, int | str]]:
    bindings: list[dict[str, int | str]] = []
    for index, ip in enumerate(secondary_ipv6s):
        bindings.append(
            {
                "ip": ip,
                "port": PROXY_START_PORT + index,
                "table": POLICY_TABLE_BASE + index + 1,
            }
        )
    return bindings


def render_config_script(
    primary_ipv6: str,
    secondary_ipv6s: list[str],
    gateway_ipv6: str,
    prefix_len: int,
) -> str:
    bindings = build_proxy_bindings(secondary_ipv6s)
    ip_entries = "\n".join(
        f"SECONDARY_IPV6S+=({shlex.quote(item['ip'])})\n"
        f"POLICY_TABLES+=({item['table']})"
        for item in bindings
    )
    listeners_json = json.dumps(
        [
            {
                "listen_host": "::",
                "listen_port": item["port"],
                "bind_ip": item["ip"],
            }
            for item in bindings
        ],
        indent=2,
    )

    proxy_script = f"""#!/usr/bin/env python3
from __future__ import annotations

import selectors
import socket
import socketserver
import threading
from urllib.parse import urlsplit

LISTENERS = {listeners_json}
UPSTREAM_CONNECT_TIMEOUT = 8


def relay_bidirectional(client: socket.socket, upstream: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    sockets = (client, upstream)
    try:
        while True:
            events = selector.select(timeout=60)
            if not events:
                break
            for key, _ in events:
                src = key.fileobj
                dst = key.data
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    finally:
        selector.close()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


class ProxyHandler(socketserver.StreamRequestHandler):
    bind_ip = None

    def handle(self) -> None:
        request_line = self.rfile.readline(65536)
        if not request_line:
            return
        try:
            method, target, version = request_line.decode("iso-8859-1").strip().split(" ", 2)
        except ValueError:
            self.wfile.write(b"HTTP/1.1 400 Bad Request\\r\\nConnection: close\\r\\n\\r\\n")
            return

        header_map = {{}}
        while True:
            line = self.rfile.readline(65536)
            if line in (b"\\r\\n", b"\\n", b""):
                break
            text = line.decode("iso-8859-1")
            if ":" in text:
                name, value = text.split(":", 1)
                header_map[name.strip().lower()] = value.strip()

        if method.upper() == "CONNECT":
            if target.startswith("["):
                close_bracket = target.find("]")
                if close_bracket == -1:
                    self.wfile.write(
                        b"HTTP/1.1 400 Bad Request\\r\\nConnection: close\\r\\n\\r\\nInvalid IPv6 literal\\n"
                    )
                    return
                host = target[1:close_bracket]
                port_text = (
                    target[close_bracket + 2:]
                    if len(target) > close_bracket + 1 and target[close_bracket + 1] == ":"
                    else ""
                )
                port = int(port_text or "443")
            else:
                host, _, port_text = target.rpartition(":")
                if not host:
                    host = target
                    port_text = ""
                port = int(port_text or "443")
            self.handle_connect(host, port)
            return

        self.handle_forward(method, target, version, header_map)

    def create_upstream(self, host: str, port: int) -> socket.socket:
        errors = []
        candidates = []

        for family, label in ((socket.AF_INET6, "IPv6"), (socket.AF_INET, "IPv4")):
            try:
                infos = socket.getaddrinfo(
                    host,
                    port,
                    family=family,
                    type=socket.SOCK_STREAM,
                )
            except socket.gaierror as exc:
                errors.append(f"{{label}} DNS: {{exc}}")
                continue
            candidates.extend((label, info) for info in infos)

        for label, (family, socktype, proto, _, sockaddr) in candidates:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(UPSTREAM_CONNECT_TIMEOUT)
                if family == socket.AF_INET6:
                    sock.bind((self.bind_ip, 0, 0, 0))
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                errors.append(f"{{label}}: {{exc}}")
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        detail = "; ".join(errors) or "no address resolved"
        raise OSError(detail)

    def handle_connect(self, host: str, port: int) -> None:
        try:
            upstream = self.create_upstream(host, port)
        except OSError as exc:
            self.wfile.write(
                f"HTTP/1.1 502 Bad Gateway\\r\\nConnection: close\\r\\n\\r\\n{{exc}}\\n".encode("utf-8")
            )
            return
        self.wfile.write(b"HTTP/1.1 200 Connection Established\\r\\n\\r\\n")
        self.wfile.flush()
        relay_bidirectional(self.connection, upstream)

    def handle_forward(self, method: str, target: str, version: str, header_map: dict[str, str]) -> None:
        parsed = urlsplit(target)
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        else:
            host_header = header_map.get("host", "")
            if not host_header:
                self.wfile.write(
                    b"HTTP/1.1 400 Bad Request\\r\\nConnection: close\\r\\n\\r\\nMissing Host header\\n"
                )
                return
            if host_header.startswith("["):
                close_bracket = host_header.find("]")
                if close_bracket == -1:
                    self.wfile.write(
                        b"HTTP/1.1 400 Bad Request\\r\\nConnection: close\\r\\n\\r\\nInvalid Host header\\n"
                    )
                    return
                host = host_header[1:close_bracket]
                port_text = (
                    host_header[close_bracket + 2:]
                    if len(host_header) > close_bracket + 1 and host_header[close_bracket + 1] == ":"
                    else ""
                )
                port = int(port_text or "80")
            else:
                if ":" in host_header:
                    host, port_text = host_header.rsplit(":", 1)
                    port = int(port_text)
                else:
                    host = host_header
                    port = 80
            path = target

        content_length = int(header_map.get("content-length", "0") or "0")
        body = self.rfile.read(content_length) if content_length else b""

        outgoing_headers = []
        for name, value in header_map.items():
            if name in {{"proxy-connection", "proxy-authorization", "connection"}}:
                continue
            outgoing_headers.append(f"{{name.title()}}: {{value}}\\r\\n")
        outgoing_headers.append("Connection: close\\r\\n")

        request_bytes = (
            f"{{method}} {{path}} {{version}}\\r\\n".encode("iso-8859-1")
            + "".join(outgoing_headers).encode("iso-8859-1")
            + b"\\r\\n"
            + body
        )

        try:
            upstream = self.create_upstream(host, port)
        except OSError as exc:
            self.wfile.write(
                f"HTTP/1.1 502 Bad Gateway\\r\\nConnection: close\\r\\n\\r\\n{{exc}}\\n".encode("utf-8")
            )
            return

        try:
            upstream.sendall(request_bytes)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    address_family = socket.AF_INET6


def serve_listener(listener: dict[str, object]) -> ThreadingTCPServer:
    bind_ip = str(listener["bind_ip"])
    port = int(listener["listen_port"])

    class BoundProxyHandler(ProxyHandler):
        pass

    BoundProxyHandler.bind_ip = bind_ip
    return ThreadingTCPServer((str(listener["listen_host"]), port), BoundProxyHandler)


def main() -> int:
    servers = [serve_listener(listener) for listener in LISTENERS]
    threads = []
    for server, listener in zip(servers, LISTENERS):
        print(f"LISTEN {{listener['listen_host']}}:{{listener['listen_port']}} via {{listener['bind_ip']}}", flush=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

    return f"""#!/bin/bash
set -euo pipefail

INTERFACE={shlex.quote(VM_INTERFACE)}
PRIMARY_IPV6={shlex.quote(primary_ipv6)}
IPV6_GATEWAY={shlex.quote(gateway_ipv6)}
PREFIX_LEN={prefix_len}
ROUTE_SCRIPT_PATH={shlex.quote(ROUTE_SCRIPT)}
PROXY_SCRIPT_PATH={shlex.quote(PROXY_SCRIPT)}
ROUTE_SERVICE_NAME={shlex.quote(ROUTE_SERVICE)}
PROXY_SERVICE_NAME={shlex.quote(PROXY_SERVICE)}
POLICY_TABLE_BASE={POLICY_TABLE_BASE}

install -d "$(dirname "$ROUTE_SCRIPT_PATH")"
install -d "$(dirname "$PROXY_SCRIPT_PATH")"

for table in $(seq $((POLICY_TABLE_BASE + 1)) $((POLICY_TABLE_BASE + 512))); do
  while ip -6 rule del table "$table" 2>/dev/null; do :; done
  ip -6 route flush table "$table" 2>/dev/null || true
done

systemctl stop "$PROXY_SERVICE_NAME" 2>/dev/null || true
systemctl stop "$ROUTE_SERVICE_NAME" 2>/dev/null || true
pkill -f {shlex.quote(PROXY_SCRIPT)} 2>/dev/null || true
ip -6 addr flush dev "$INTERFACE" scope global 2>/dev/null || true

cat > "$ROUTE_SCRIPT_PATH" <<'EOF_ROUTE'
#!/bin/bash
set -euo pipefail

INTERFACE={shlex.quote(VM_INTERFACE)}
PRIMARY_IPV6={shlex.quote(primary_ipv6)}
IPV6_GATEWAY={shlex.quote(gateway_ipv6)}
PREFIX_LEN={prefix_len}
SECONDARY_IPV6S=()
POLICY_TABLES=()
{ip_entries}

sysctl -w net.ipv6.conf.all.disable_ipv6=0 >/dev/null
sysctl -w net.ipv6.conf.default.disable_ipv6=0 >/dev/null
sysctl -w net.ipv6.conf."$INTERFACE".disable_ipv6=0 >/dev/null

ip -6 addr flush dev "$INTERFACE" scope global 2>/dev/null || true
ip -6 addr add "$PRIMARY_IPV6/$PREFIX_LEN" dev "$INTERFACE" nodad 2>/dev/null || true
ip -6 route replace default via "$IPV6_GATEWAY" dev "$INTERFACE" src "$PRIMARY_IPV6"

for idx in "${{!SECONDARY_IPV6S[@]}}"; do
  ip="${{SECONDARY_IPV6S[$idx]}}"
  table="${{POLICY_TABLES[$idx]}}"
  ip -6 addr add "$ip/$PREFIX_LEN" dev "$INTERFACE" nodad 2>/dev/null || true
  ip -6 route replace default via "$IPV6_GATEWAY" dev "$INTERFACE" src "$ip" table "$table"
  ip -6 rule show | grep -q "from $ip lookup $table" || ip -6 rule add from "$ip" table "$table"
done
EOF_ROUTE
chmod 755 "$ROUTE_SCRIPT_PATH"

cat > "$PROXY_SCRIPT_PATH" <<'EOF_PROXY'
{proxy_script}
EOF_PROXY
chmod 755 "$PROXY_SCRIPT_PATH"

cat > /etc/systemd/system/"$ROUTE_SERVICE_NAME" <<EOF_UNIT
[Unit]
Description=CloudPe IPv6 secondary address and policy routing setup
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$ROUTE_SCRIPT_PATH

[Install]
WantedBy=multi-user.target
EOF_UNIT

cat > /etc/systemd/system/"$PROXY_SERVICE_NAME" <<EOF_UNIT
[Unit]
Description=CloudPe Python IPv6 proxy service
After=network-online.target $ROUTE_SERVICE_NAME
Wants=network-online.target
Requires=$ROUTE_SERVICE_NAME

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $PROXY_SCRIPT_PATH
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF_UNIT

# Ensure IPv6 DNS is configured (backup original first)
if [ ! -f /etc/resolv.conf.backup ]; then
  cp /etc/resolv.conf /etc/resolv.conf.backup 2>/dev/null || true
fi

# Use reliable IPv6 DNS servers (Cloudflare + Google)
cat > /etc/resolv.conf <<EOF_DNS
nameserver 2606:4700:4700::1111
nameserver 2606:4700:4700::1001
nameserver 2001:4860:4860::8888
nameserver 2001:4860:4860::8844
EOF_DNS

"$ROUTE_SCRIPT_PATH"
systemctl daemon-reload
systemctl enable "$ROUTE_SERVICE_NAME" "$PROXY_SERVICE_NAME"
systemctl restart "$ROUTE_SERVICE_NAME"
systemctl restart "$PROXY_SERVICE_NAME"
systemctl --no-pager --full status "$PROXY_SERVICE_NAME" || true

printf '\\n== IPv6 addresses ==\\n'
ip -6 addr show dev "$INTERFACE"
printf '\\n== IPv6 routes ==\\n'
ip -6 route
printf '\\n== Listening ports ==\\n'
ss -lnt
echo "PROXY_READY"
"""


def apply_config(
    primary_ipv6: str,
    secondary_ipv6s: list[str],
    gateway_ipv6: str,
    prefix_len: int,
) -> tuple[bool, str]:
    """Apply network configuration to the VM (SSH + systemd services)."""
    print(
        f"    phase=remote-apply start ips={len(secondary_ipv6s)} "
        f"ports={PROXY_START_PORT}-{PROXY_START_PORT + len(secondary_ipv6s) - 1}",
        flush=True,
    )
    script = render_config_script(primary_ipv6, secondary_ipv6s, gateway_ipv6, prefix_len)
    try:
        completed = ssh_script(script)
    except RuntimeError as exc:
        print(f"    phase=remote-apply failed error={exc}", flush=True)
        return False, str(exc)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "remote apply failed"
        print(f"    phase=remote-apply failed detail={detail}", flush=True)
        return False, detail
    if "PROXY_READY" not in completed.stdout:
        print("    phase=remote-apply failed detail=remote apply did not report readiness", flush=True)
        return False, "remote apply did not report readiness"
    print("    phase=remote-apply done", flush=True)

    # Give services a moment to fully initialize before verification
    print("    waiting for services to stabilize...", flush=True)
    time.sleep(3)

    return True, "ok"


def verify_single_proxy(proxy_ip: str, proxy_port: int) -> tuple[bool, str]:
    """Verify a single proxy works by connecting through it."""
    verify_script = f"""python3 - <<'PY'
import socket
import sys

bind_ip = {json.dumps(proxy_ip)}
proxy_port = {proxy_port}
host = {json.dumps(REMOTE_IP_TEST_HOST)}

try:
    with socket.create_connection((bind_ip, proxy_port), timeout=20) as sock:
        request = (
            f"GET http://{{host}}/ HTTP/1.1\\r\\n"
            f"Host: {{host}}\\r\\n"
            "Connection: close\\r\\n\\r\\n"
        ).encode("ascii")
        sock.sendall(request)
        response = sock.recv(4096)
    if b"HTTP/1.1 200" not in response and b"HTTP/1.0 200" not in response:
        print(f"FAIL: bad response -> {{response[:80]!r}}", file=sys.stderr)
        sys.exit(1)
except Exception as exc:
    print(f"FAIL: {{exc}}", file=sys.stderr)
    sys.exit(1)
PY"""
    print(f"    phase=remote-verify start proxy=[{proxy_ip}]:{proxy_port}", flush=True)
    try:
        verified = ssh_script(verify_script)
    except RuntimeError as exc:
        print(f"    phase=remote-verify failed error={exc}", flush=True)
        return False, str(exc)
    if verified.returncode != 0:
        detail = verified.stderr.strip() or verified.stdout.strip() or "remote verify failed"
        print(f"    phase=remote-verify failed detail={detail}", flush=True)
        return False, detail
    print("    phase=remote-verify done", flush=True)
    return True, "ok"


def apply_and_verify(
    primary_ipv6: str,
    secondary_ipv6s: list[str],
    gateway_ipv6: str,
    prefix_len: int,
) -> tuple[bool, str]:
    """Verify ALL proxies work (with retries for reliability)."""
    verified_secondary_ipv6s: list[str] = []
    for index, ip in enumerate(secondary_ipv6s):
        proxy_port = PROXY_START_PORT + index
        last_error = "proxy verification did not run"

        for attempt in range(2):
            ok, detail = verify_single_proxy(ip, proxy_port)
            if ok:
                verified_secondary_ipv6s.append(ip)
                break
            last_error = detail
            if attempt == 0:
                time.sleep(RETRY_DELAY_SECONDS)
        else:
            print(
                f"    phase=remote-verify skipping proxy=[{ip}]:{proxy_port} detail={last_error}",
                flush=True,
            )

    if not verified_secondary_ipv6s:
        return False, "all proxies failed final verification"

    if len(verified_secondary_ipv6s) != len(secondary_ipv6s):
        ok, detail = apply_config(primary_ipv6, verified_secondary_ipv6s, gateway_ipv6, prefix_len)
        if not ok:
            return False, detail

    secondary_ipv6s[:] = verified_secondary_ipv6s

    return True, "ok"


def allocate_one(
    token: str,
    network_url: str,
    port_id: str,
    subnet_id: str,
    primary_ipv6: str,
    gateway_ipv6: str,
    prefix_len: int,
    working_ips: list[str],
    attempted_ips: set[str],
) -> tuple[str | None, str]:
    latest_port = api(token, network_url, "GET", f"/v2.0/ports/{port_id}")["port"]
    before_fixed_ips = normalize_fixed_ips(latest_port)
    updated_port = api(
        token,
        network_url,
        "PUT",
        f"/v2.0/ports/{port_id}",
        {"port": {"fixed_ips": [*before_fixed_ips, {"subnet_id": subnet_id}]}},
    )["port"]

    before_ips = {item["ip_address"] for item in before_fixed_ips}
    new_ips = [
        item["ip_address"]
        for item in normalize_fixed_ips(updated_port)
        if item["subnet_id"] == subnet_id and item["ip_address"] not in before_ips
    ]
    if len(new_ips) != 1:
        raise RuntimeError(f"Expected exactly one newly allocated IPv6, got {new_ips}")

    fresh_ip = new_ips[0]
    if fresh_ip in attempted_ips:
        # Don't deallocate - prevents CloudPe from recycling it immediately
        # Will be cleaned up at end with all other unwanted IPs
        return None, "previously attempted"

    attempted_ips.add(fresh_ip)
    desired_secondary_ipv6s = [*working_ips, fresh_ip]
    deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
    last_detail = "proxy health check did not run"
    while time.monotonic() < deadline:
        # Apply config with ALL IPs (needed for policy routing)
        ok, detail = apply_config(primary_ipv6, desired_secondary_ipv6s, gateway_ipv6, prefix_len)
        if not ok:
            last_detail = detail
            time.sleep(RETRY_DELAY_SECONDS)
            continue

        # But only verify the NEW proxy (not re-verify already-working ones)
        fresh_proxy_port = PROXY_START_PORT + len(working_ips)
        ok, detail = verify_single_proxy(fresh_ip, fresh_proxy_port)
        if ok:
            return fresh_ip, "ok"
        last_detail = detail
        time.sleep(RETRY_DELAY_SECONDS)

    # Don't deallocate failed IP - prevents CloudPe from recycling it immediately
    # Will be cleaned up at end with all other unwanted IPs
    print(f"    health check failed for {fresh_ip}; skipping", flush=True)
    return None, last_detail


def main() -> None:
    _lock = acquire_lock()
    load_dotenv()

    state = load_state()
    persisted_current_ips = state["current_ips"]
    persisted_used_ips = state["used_ips"]

    ssh_key_path = Path(SSH_KEY_PATH).expanduser()
    if not ssh_key_path.exists() or not ssh_key_path.is_file():
        raise RuntimeError(f"SSH key not found: {ssh_key_path}")

    print("Authenticating to CloudPe")
    token, network_url = authenticate()

    print(f"Finding port for VM {VM_SERVER_ID}")
    port_query = {"device_id": VM_SERVER_ID, "network_id": TARGET_NETWORK_ID}
    ports = api(token, network_url, "GET", f"/v2.0/ports?{parse.urlencode(port_query)}").get("ports", [])
    if not ports:
        raise RuntimeError(f"No port found for server {VM_SERVER_ID}")
    port_id = ports[0]["id"]
    initial_fixed_ips = normalize_fixed_ips(ports[0])
    print(f"Port: {port_id}")

    normalized_primary_ipv6 = normalize_ipv6(PRIMARY_IPV6)
    if not any(
        item["ip_address"] == normalized_primary_ipv6
        for item in initial_fixed_ips
        if ipaddress.ip_address(item["ip_address"]).version == 6
    ):
        raise RuntimeError(f"Primary IPv6 {normalized_primary_ipv6} not found on VM port {port_id}")

    attempted_ips = set(persisted_current_ips) | set(persisted_used_ips)

    print("Fetching IPv6 subnets")
    raw_subnets = api(
        token,
        network_url,
        "GET",
        f"/v2.0/subnets?{parse.urlencode({'network_id': TARGET_NETWORK_ID})}",
    ).get("subnets", [])
    subnets: list[dict] = []
    for subnet in raw_subnets:
        if int(subnet.get("ip_version", 0)) != 6:
            continue
        gateway_ip = subnet.get("gateway_ip")
        cidr = subnet.get("cidr")
        subnet_id = subnet.get("id")
        if not gateway_ip or not cidr or not subnet_id:
            continue
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        subnets.append(subnet)
    if not subnets:
        raise RuntimeError("No IPv6 subnets found on the configured network")

    working_ips: list[str] = []
    chosen_subnet: dict | None = None
    chosen_gateway_ipv6: str | None = None
    chosen_prefix_len: int | None = None

    for subnet in subnets:
        subnet_id = str(subnet["id"])
        gateway_ipv6 = normalize_ipv6(str(subnet["gateway_ip"]))
        prefix_len = int(ipaddress.ip_network(str(subnet["cidr"]), strict=False).prefixlen)

        print(f"Trying subnet {subnet_id} cidr={subnet['cidr']} gateway={gateway_ipv6} primary={normalized_primary_ipv6}")
        attempts = 0
        consecutive_failures = 0

        while len(working_ips) < PROXY_COUNT and attempts < MAX_ALLOCATION_ATTEMPTS:
            attempts += 1
            print(f"  attempt {attempts}/{MAX_ALLOCATION_ATTEMPTS} working={len(working_ips)}/{PROXY_COUNT}")
            try:
                fresh_ip, reason = allocate_one(
                    token,
                    network_url,
                    port_id,
                    subnet_id,
                    normalized_primary_ipv6,
                    gateway_ipv6,
                    prefix_len,
                    working_ips,
                    attempted_ips,
                )
            except RuntimeError as exc:
                print(f"  allocation error: {exc}")
                break

            if fresh_ip is None:
                print(f"  skipped/failed: {reason}")
                consecutive_failures += 1
                if consecutive_failures >= 20:
                    print("  20 consecutive failures, moving to next subnet")
                    break
                continue

            working_ips.append(fresh_ip)
            print(f"  proxy ok [{fresh_ip}]:{PROXY_START_PORT + len(working_ips) - 1}")
            chosen_subnet = subnet
            chosen_gateway_ipv6 = gateway_ipv6
            chosen_prefix_len = prefix_len
            consecutive_failures = 0

        if len(working_ips) >= PROXY_COUNT:
            break
        if working_ips:
            print(f"Subnet exhausted with {len(working_ips)}/{PROXY_COUNT} proxies, trying next")

    if not working_ips:
        raise RuntimeError("No working IPv6 proxies could be provisioned")
    if chosen_subnet is None or chosen_gateway_ipv6 is None or chosen_prefix_len is None:
        raise RuntimeError("Internal error: working IPv6s were found but no subnet was selected")

    if len(working_ips) < PROXY_COUNT:
        print(f"Partial success: {len(working_ips)}/{PROXY_COUNT} working proxies")

    print("Trimming CloudPe port to working IPs only")
    latest_port = api(token, network_url, "GET", f"/v2.0/ports/{port_id}")["port"]
    working_set = set(working_ips)
    kept_fixed_ips = [
        item
        for item in normalize_fixed_ips(latest_port)
        if item["subnet_id"] != chosen_subnet["id"]
        or item["ip_address"] in {normalized_primary_ipv6, *working_set}
    ]
    api(
        token,
        network_url,
        "PUT",
        f"/v2.0/ports/{port_id}",
        {"port": {"fixed_ips": kept_fixed_ips}},
    )

    print("Applying final configuration")
    final_candidate_ips = [*working_ips]
    ok, detail = apply_and_verify(
        normalized_primary_ipv6,
        working_ips,
        chosen_gateway_ipv6,
        chosen_prefix_len,
    )
    if not ok:
        raise RuntimeError(f"Final deploy/verify failed: {detail}")
    working_set = set(working_ips)
    final_skipped_ips = [ip for ip in final_candidate_ips if ip not in working_set]

    if final_skipped_ips:
        print(f"Final verification skipped {len(final_skipped_ips)} proxies; trimming CloudPe port again")
        latest_port = api(token, network_url, "GET", f"/v2.0/ports/{port_id}")["port"]
        kept_fixed_ips = [
            item
            for item in normalize_fixed_ips(latest_port)
            if item["subnet_id"] != chosen_subnet["id"]
            or item["ip_address"] in {normalized_primary_ipv6, *working_set}
        ]
        api(
            token,
            network_url,
            "PUT",
            f"/v2.0/ports/{port_id}",
            {"port": {"fixed_ips": kept_fixed_ips}},
        )

    updated_used_ips = [*persisted_used_ips]
    for ip in persisted_current_ips:
        if ip not in working_set and ip not in updated_used_ips:
            updated_used_ips.append(ip)
    save_state(working_ips, updated_used_ips)

    proxies = [f"[{ip}]:{PROXY_START_PORT + index}" for index, ip in enumerate(working_ips)]
    print("Completed successfully")
    print(
        json.dumps(
            {
                "server_id": VM_SERVER_ID,
                "port_id": port_id,
                "ssh_host": SSH_HOST,
                "subnet_id": chosen_subnet["id"],
                "cidr": chosen_subnet["cidr"],
                "primary_ipv6": normalized_primary_ipv6,
                "proxy_count": len(working_ips),
                "working_ips": working_ips,
                "proxies": proxies,
                "proxy_pool": [
                    {"host": ip, "port": PROXY_START_PORT + index}
                    for index, ip in enumerate(working_ips)
                ],
                "state_file": str(STATE_FILE),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
