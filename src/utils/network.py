"""Network utilities for resolving VM/CT IP addresses."""

import socket

from ..api.exceptions import PVECliError


def find_free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def is_ipv6_literal(host: str) -> bool:
    """Return True if host is an IPv6 address literal (with or without brackets)."""
    if not host:
        return False
    raw = host.strip("[]")
    if raw.count(":") < 2:
        return False
    try:
        socket.inet_pton(socket.AF_INET6, raw)
        return True
    except (OSError, ValueError):
        return False


def format_host_for_url(host: str) -> str:
    """Wrap IPv6 literals in brackets for use in URLs.

    Leaves IPv4 / hostnames / already-bracketed IPv6 unchanged.
    """
    if not host:
        return host
    if host.startswith("[") and host.endswith("]"):
        return host
    if is_ipv6_literal(host):
        return f"[{host}]"
    return host


def resolve_node_host(profile_config) -> str:
    """Extract hostname from profile config (without scheme or port)."""
    host = profile_config.host
    if "://" in host:
        host = host.split("://", 1)[1]
    # Bracketed IPv6 → strip brackets, drop optional trailing port
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[1:end]
    # IPv6 literal without brackets → return as-is (no port stripping possible)
    if is_ipv6_literal(host):
        return host
    # IPv4 / hostname → cut at first colon if any
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def _extract_vm_ip(interfaces: list[dict]) -> str:
    """Extract the best IP from QEMU Guest Agent interfaces.

    Format: [{"name": "eth0", "ip-addresses": [{"ip-address": "x.x.x.x", "ip-address-type": "ipv4"}]}]
    """
    ipv4_addrs = []
    ipv6_addrs = []

    for iface in interfaces:
        if iface.get("name", "") == "lo":
            continue
        for addr in iface.get("ip-addresses", []):
            ip = addr.get("ip-address", "")
            ip_type = addr.get("ip-address-type", "")
            if ip_type == "ipv4":
                if not ip.startswith("169.254."):
                    ipv4_addrs.append(ip)
            elif ip_type == "ipv6":
                if not ip.startswith("fe80:"):
                    ipv6_addrs.append(ip)

    if ipv4_addrs:
        return ipv4_addrs[0]
    if ipv6_addrs:
        return ipv6_addrs[0]
    raise PVECliError("No IP address found")


def _extract_ct_ip(interfaces: list[dict]) -> str:
    """Extract the best IP from LXC container interfaces.

    Format: [{"name": "eth0", "inet": "10.0.0.5/24", "inet6": "fe80::1/64"}]
    """
    ipv4_addrs = []
    ipv6_addrs = []

    for iface in interfaces:
        if iface.get("name", "") == "lo":
            continue
        inet = iface.get("inet", "")
        if inet:
            ip = inet.split("/")[0]
            if not ip.startswith("169.254."):
                ipv4_addrs.append(ip)
        inet6 = iface.get("inet6", "")
        if inet6:
            ip = inet6.split("/")[0]
            if not ip.startswith("fe80:"):
                ipv6_addrs.append(ip)

    if ipv4_addrs:
        return ipv4_addrs[0]
    if ipv6_addrs:
        return ipv6_addrs[0]
    raise PVECliError("No IP address found")


async def resolve_vm_ip(client, node: str, vmid: int) -> str:
    """Resolve VM IP via QEMU Guest Agent."""
    interfaces = await client.get_vm_interfaces(node, vmid)
    if not interfaces:
        raise PVECliError(
            f"Cannot resolve IP for VM {vmid}: QEMU Guest Agent not available. "
            f"Use 'pvecli vm vnc {vmid}' instead."
        )
    try:
        return _extract_vm_ip(interfaces)
    except PVECliError:
        raise PVECliError(f"No IP address found for VM {vmid}")


async def resolve_ct_ip(client, node: str, ctid: int) -> str:
    """Resolve container IP via interfaces API."""
    interfaces = await client.get_container_interfaces(node, ctid)
    if not interfaces:
        raise PVECliError(f"No network interfaces found for CT {ctid}")
    try:
        return _extract_ct_ip(interfaces)
    except PVECliError:
        raise PVECliError(f"No IP address found for CT {ctid}")
