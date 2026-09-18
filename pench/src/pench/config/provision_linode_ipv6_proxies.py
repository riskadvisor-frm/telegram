from __future__ import annotations

import base64
import fcntl
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# Linode VM configurations
LINODE_VMS = {
    "vm1": {
        "host": "194.195.116.99",
        "ipv6_primary": "2400:8904::2000:77ff:fe2f:aa7f",
        "ipv6_range": "2400:8904:e002:b200::/56",
    },
    "vm2": {
        "host": "172.235.28.186",
        "ipv6_primary": "2600:3c08::2000:3fff:fe9e:5db7",
        "ipv6_range": "2600:3c08:e002:9000::/56",
    },
    "vm3": {
        "host": "172.236.187.244",
        "ipv6_primary": "2600:3c16::2000:79ff:feff:0aad",
        "ipv6_range": "2600:3c16:e002:1800::/56",
    },
}

# SSH connection settings
SSH_USER = "root"
SSH_KEY_PATH = str(Path("~/.ssh/ovh_vm").expanduser())
SSH_PORT = 22
SSH_CONNECT_TIMEOUT_SECONDS = 10
SSH_COMMAND_TIMEOUT_SECONDS = 120

# Network interface configuration
NETWORK_INTERFACE = "eth0"
IPV6_GATEWAY = "fe80::1"
IPV6_PREFIX_LENGTH = 64

# Proxy pool configuration
PROXIES_PER_VM = 15
PROXY_PORT_START = 3001
PROXY_VERIFY_TIMEOUT_SECONDS = 120
PROXY_RETRY_DELAY_SECONDS = 2
# Start allocating IPs from offset 16 to avoid conflicts with primary/gateway addresses
IPV6_ALLOCATION_OFFSET = 16

# File system paths
POOL_DIR = Path(__file__).parent / "proxy_pools"

# Remote systemd service paths
REMOTE_ROUTE_SCRIPT_PATH = "/usr/local/sbin/linode-ipv6-routes.sh"
REMOTE_PROXY_SCRIPT_PATH = "/usr/local/bin/linode-ipv6-http-proxy.py"
REMOTE_ROUTE_SERVICE_NAME = "linode-ipv6-routes.service"
REMOTE_PROXY_SERVICE_NAME = "linode-ipv6-proxy.service"

# Linux policy routing table allocation
# Using range 22001-22512 for per-IP policy routing tables
POLICY_ROUTING_TABLE_BASE = 22000

# Provider identification for proxy tracking
PROXY_PROVIDER_NAME = "linode"

# External IPv6 test endpoints for proxy verification
IPV6_TEST_ENDPOINTS = ("api6.ipify.org", "ipv6.icanhazip.com")


def normalize_ipv6(ip: str) -> str:
    value = ipaddress.ip_address(ip)
    if value.version != 6:
        raise ValueError(f"Expected IPv6 address, got: {ip}")
    return str(value)


def load_pool(pool_file: Path) -> list[str]:
    """Load allocated IPv6 addresses from proxy pool file."""
    if not pool_file.exists():
        return []

    pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
    ips = pool_data.get("ips", [])
    if not isinstance(ips, list):
        raise RuntimeError(f"Invalid pool file: {pool_file}")

    for ip in ips:
        if not isinstance(ip, str):
            raise RuntimeError(f"Invalid pool file: {pool_file}")
        try:
            normalize_ipv6(ip)
        except ValueError as exc:
            raise RuntimeError(f"Invalid pool file: {pool_file}") from exc

    return ips


def save_pool(pool_file: Path, ips: list[str]) -> None:
    """Save allocated IPv6 addresses to proxy pool file."""
    seen: set[str] = set()
    deduped: list[str] = []

    for ip in ips:
        if ip in seen:
            continue
        seen.add(ip)
        deduped.append(ip)

    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text(
        json.dumps(
            {"ips": deduped},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def ssh_script(ssh_host: str, script: str) -> subprocess.CompletedProcess[str]:
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
                f"{SSH_USER}@{ssh_host}",
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


def get_proxy_auth_header() -> str:
    token = os.getenv("LINODE_TOKEN", "")
    if not token:
        raise RuntimeError("Set LINODE_TOKEN before provisioning Linode proxies")

    encoded = base64.b64encode(f"linode:{token}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def render_config_script(primary_ipv6: str, secondary_ipv6s: list[str]) -> str:
    proxy_auth_header = get_proxy_auth_header()
    bindings = [
        {
            "ip": ip,
            "port": PROXY_PORT_START + index,
            "table": POLICY_ROUTING_TABLE_BASE + index + 1,
        }
        for index, ip in enumerate(secondary_ipv6s)
    ]
    ip_entries = "\n".join(
        f"SECONDARY_IPV6S+=({shlex.quote(binding['ip'])})\n"
        f"POLICY_TABLES+=({binding['table']})"
        for binding in bindings
    )
    listeners_json = json.dumps(
        [
            {
                "listen_host": binding["ip"],
                "listen_port": binding["port"],
                "bind_ip": binding["ip"],
            }
            for binding in bindings
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
EXPECTED_PROXY_AUTH = {json.dumps(proxy_auth_header)}


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

        if header_map.get("proxy-authorization", "") != EXPECTED_PROXY_AUTH:
            self.wfile.write(
                b"HTTP/1.1 407 Proxy Authentication Required\\r\\n"
                b"Proxy-Authenticate: Basic realm=\\"Pench Linode Proxy\\"\\r\\n"
                b"Connection: close\\r\\n\\r\\n"
            )
            return

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
                infos = socket.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
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

        raise OSError("; ".join(errors) or "no address resolved")

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

    def handle_forward(
        self,
        method: str,
        target: str,
        version: str,
        header_map: dict[str, str],
    ) -> None:
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
        print(
            f"LISTEN {{listener['listen_host']}}:{{listener['listen_port']}} via {{listener['bind_ip']}}",
            flush=True,
        )
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

INTERFACE={shlex.quote(NETWORK_INTERFACE)}
PRIMARY_IPV6={shlex.quote(primary_ipv6)}
IPV6_GATEWAY={shlex.quote(IPV6_GATEWAY)}
PREFIX_LEN={IPV6_PREFIX_LENGTH}
ROUTE_SCRIPT_PATH={shlex.quote(REMOTE_ROUTE_SCRIPT_PATH)}
PROXY_SCRIPT_PATH={shlex.quote(REMOTE_PROXY_SCRIPT_PATH)}
ROUTE_SERVICE_NAME={shlex.quote(REMOTE_ROUTE_SERVICE_NAME)}
PROXY_SERVICE_NAME={shlex.quote(REMOTE_PROXY_SERVICE_NAME)}
POLICY_TABLE_BASE={POLICY_ROUTING_TABLE_BASE}

install -d "$(dirname "$ROUTE_SCRIPT_PATH")"
install -d "$(dirname "$PROXY_SCRIPT_PATH")"

for table in $(seq $((POLICY_TABLE_BASE + 1)) $((POLICY_TABLE_BASE + 512))); do
  while ip -6 rule del table "$table" 2>/dev/null; do :; done
  ip -6 route flush table "$table" 2>/dev/null || true
done

systemctl stop "$PROXY_SERVICE_NAME" 2>/dev/null || true
pkill -f {shlex.quote(REMOTE_PROXY_SCRIPT_PATH)} 2>/dev/null || true

cat > "$ROUTE_SCRIPT_PATH" <<'EOF_ROUTE'
#!/bin/bash
set -euo pipefail

INTERFACE={shlex.quote(NETWORK_INTERFACE)}
PRIMARY_IPV6={shlex.quote(primary_ipv6)}
IPV6_GATEWAY={shlex.quote(IPV6_GATEWAY)}
PREFIX_LEN={IPV6_PREFIX_LENGTH}
POLICY_TABLE_BASE={POLICY_ROUTING_TABLE_BASE}
SECONDARY_IPV6S=()
POLICY_TABLES=()
{ip_entries}

for table in $(seq $((POLICY_TABLE_BASE + 1)) $((POLICY_TABLE_BASE + 512))); do
  while ip -6 rule del table "$table" 2>/dev/null; do :; done
  ip -6 route flush table "$table" 2>/dev/null || true
done

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
Description=Linode IPv6 secondary address and policy routing setup
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
Description=Linode Python IPv6 proxy service
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

"$ROUTE_SCRIPT_PATH"
systemctl daemon-reload
systemctl enable "$ROUTE_SERVICE_NAME" "$PROXY_SERVICE_NAME"
systemctl restart "$ROUTE_SERVICE_NAME"
systemctl restart "$PROXY_SERVICE_NAME"
echo "PROXY_READY"
"""


def provision_vm(vm: str) -> None:
    config = LINODE_VMS.get(vm)
    if config is None:
        raise RuntimeError(f"Unknown VM: {vm}. Available: {list(LINODE_VMS)}")

    ssh_host = config["host"]
    try:
        primary_ipv6 = normalize_ipv6(config["ipv6_primary"])
    except ValueError as exc:
        raise RuntimeError(f"Invalid primary IPv6 address: {config['ipv6_primary']}") from exc

    try:
        network = ipaddress.ip_network(config["ipv6_range"], strict=False)
    except ValueError as exc:
        raise RuntimeError(f"Invalid IPv6 range: {config['ipv6_range']}") from exc
    if network.prefixlen not in (56, 64):
        raise RuntimeError(f"IPv6 range must be /56 or /64, got /{network.prefixlen}")

    pool_file = POOL_DIR / f"linode_proxy_pool_{vm}.json"
    lock_file = POOL_DIR / f"provision_linode_ipv6_proxies_{vm}.lock"
    POOL_DIR.mkdir(parents=True, exist_ok=True)

    lock_handle = lock_file.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise RuntimeError(f"Another run is already active: {lock_file}") from exc
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(f"{os.getpid()}\n")
    lock_handle.flush()

    ssh_key_path = Path(SSH_KEY_PATH).expanduser()
    if not ssh_key_path.exists() or not ssh_key_path.is_file():
        raise RuntimeError(f"SSH key not found: {ssh_key_path}")

    current_ips = load_pool(pool_file)
    attempted_ips = set(current_ips)

    range_cidr = str(network)
    print(f"VM: {vm}")
    print(f"SSH host: {ssh_host}")
    print(f"Primary IPv6: {primary_ipv6}")
    print(f"IPv6 range: {range_cidr}")

    candidate_ips: list[str] = []
    if current_ips:
        offset = (
            max(int(ipaddress.ip_address(ip)) for ip in current_ips)
            - int(network.network_address)
            + 1
        )
    else:
        offset = IPV6_ALLOCATION_OFFSET
    while len(candidate_ips) < PROXIES_PER_VM:
        max_offset = offset + len(attempted_ips) + PROXIES_PER_VM + 4096
        while offset < max_offset:
            candidate = str(network.network_address + offset)
            offset += 1
            if candidate in attempted_ips:
                continue
            attempted_ips.add(candidate)
            candidate_ips.append(candidate)
            break
        else:
            print("  skipped/failed: no sequential IPv6 candidate available", flush=True)
            break

    if not candidate_ips:
        raise RuntimeError("No IPv6 proxy candidates could be selected")

    if len(candidate_ips) < PROXIES_PER_VM:
        print(
            f"Partial candidate selection: {len(candidate_ips)}/{PROXIES_PER_VM} IPs",
            flush=True,
        )

    print(
        f"    phase=remote-apply start ips={len(candidate_ips)} "
        f"ports={PROXY_PORT_START}-{PROXY_PORT_START + len(candidate_ips) - 1}",
        flush=True,
    )
    try:
        completed = ssh_script(ssh_host, render_config_script(primary_ipv6, candidate_ips))
    except RuntimeError as exc:
        print(f"    phase=remote-apply failed error={exc}", flush=True)
        raise RuntimeError(f"Final deploy/verify failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "remote apply failed"
        print(f"    phase=remote-apply failed detail={detail}", flush=True)
        raise RuntimeError(f"Final deploy/verify failed: {detail}")
    if "PROXY_READY" not in completed.stdout:
        detail = "remote apply did not report readiness"
        print(f"    phase=remote-apply failed detail={detail}", flush=True)
        raise RuntimeError(f"Final deploy/verify failed: {detail}")
    print("    phase=remote-apply done", flush=True)
    time.sleep(3)

    proxy_auth_header = get_proxy_auth_header()
    working_ips: list[str] = []
    for index, ip in enumerate(candidate_ips):
        proxy_port = PROXY_PORT_START + index
        last_error = "proxy verification did not run"
        deadline = time.monotonic() + PROXY_VERIFY_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            verify_script = f"""python3 - <<'PY'
import socket
import sys

bind_ip = {json.dumps(ip)}
proxy_port = {proxy_port}
hosts = {json.dumps(list(IPV6_TEST_ENDPOINTS))}
proxy_auth_header = {json.dumps(proxy_auth_header)}

errors = []
for host in hosts:
    try:
        with socket.create_connection((bind_ip, proxy_port), timeout=20) as sock:
            request = (
                f"GET http://{{host}}/ HTTP/1.1\\r\\n"
                f"Host: {{host}}\\r\\n"
                f"Proxy-Authorization: {{proxy_auth_header}}\\r\\n"
                "Connection: close\\r\\n\\r\\n"
            ).encode("ascii")
            sock.sendall(request)
            response = sock.recv(4096)
        if b"HTTP/1.1 200" in response or b"HTTP/1.0 200" in response:
            sys.exit(0)
        errors.append(f"{{host}} => bad response {{response[:120]!r}}")
    except Exception as exc:
        errors.append(f"{{host}} => {{exc}}")

print("FAIL: " + "; ".join(errors), file=sys.stderr)
sys.exit(1)
PY"""
            print(f"    phase=remote-verify start proxy=[{ip}]:{proxy_port}", flush=True)
            try:
                verified = ssh_script(ssh_host, verify_script)
            except RuntimeError as exc:
                last_error = str(exc)
                print(f"    phase=remote-verify failed error={exc}", flush=True)
            else:
                if verified.returncode == 0:
                    print("    phase=remote-verify done", flush=True)
                    working_ips.append(ip)
                    break
                last_error = (
                    verified.stderr.strip()
                    or verified.stdout.strip()
                    or "remote verify failed"
                )
                print(f"    phase=remote-verify failed detail={last_error}", flush=True)

            if time.monotonic() < deadline:
                time.sleep(PROXY_RETRY_DELAY_SECONDS)
        else:
            print(
                f"    phase=remote-verify skipping proxy=[{ip}]:{proxy_port} detail={last_error}",
                flush=True,
            )

    if not working_ips:
        raise RuntimeError("Final deploy/verify failed: all proxies failed final verification")

    if len(working_ips) != len(candidate_ips):
        print(
            f"    phase=remote-apply start ips={len(working_ips)} "
            f"ports={PROXY_PORT_START}-{PROXY_PORT_START + len(working_ips) - 1}",
            flush=True,
        )
        try:
            completed = ssh_script(ssh_host, render_config_script(primary_ipv6, working_ips))
        except RuntimeError as exc:
            print(f"    phase=remote-apply failed error={exc}", flush=True)
            raise RuntimeError(f"Final deploy/verify failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "remote apply failed"
            print(f"    phase=remote-apply failed detail={detail}", flush=True)
            raise RuntimeError(f"Final deploy/verify failed: {detail}")
        if "PROXY_READY" not in completed.stdout:
            detail = "remote apply did not report readiness"
            print(f"    phase=remote-apply failed detail={detail}", flush=True)
            raise RuntimeError(f"Final deploy/verify failed: {detail}")
        print("    phase=remote-apply done", flush=True)
        time.sleep(3)

    save_pool(pool_file, working_ips)

    print("Completed successfully")
    print(
        json.dumps(
            {
                "provider": PROXY_PROVIDER_NAME,
                "ssh_host": ssh_host,
                "primary_ipv6": primary_ipv6,
                "range": range_cidr,
                "proxy_count": len(working_ips),
                # "working_ips": working_ips,
                "proxies": [
                    f"[{ip}]:{PROXY_PORT_START + index}"
                    for index, ip in enumerate(working_ips)
                ],
                # "proxy_pool": [
                #     {"host": ip, "port": PROXY_START_PORT + index}
                #     for index, ip in enumerate(working_ips)
                # ],
                "pool_file": str(pool_file),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <vm-name> [<vm-name> ...] | all", file=sys.stderr)
        print(f"Available VMs: {', '.join(LINODE_VMS)}", file=sys.stderr)
        print(f"Example: {sys.argv[0]} vm1 vm2", file=sys.stderr)
        sys.exit(1)

    vms = list(LINODE_VMS) if "all" in sys.argv[1:] else sys.argv[1:]
    failed_vms: list[str] = []

    for vm in vms:
        print(f"\n{'=' * 60}\nProvisioning: {vm}\n{'=' * 60}")
        try:
            provision_vm(vm)
            print(f"✓ {vm}: SUCCESS")
        except Exception as exc:
            print(f"✗ {vm}: FAILED - {exc}", file=sys.stderr)
            failed_vms.append(vm)

    if failed_vms:
        print(f"\nFailed VMs: {', '.join(failed_vms)}", file=sys.stderr)
        sys.exit(1)
