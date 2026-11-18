"""Find where something lives, and what it belongs to (read only).

The query type is detected from its shape: an IP or a network, a MAC, a
VMID, a disk volume, or text (name, hostname, notes). A DNS name that
resolves is searched as the IPs it resolves to. Filters narrow or replace
the query: what a guest is attached to (--on-vnet, --on-storage, --iso...)
and conditions on it (--stopped, --no-backup, --old-snapshots 30d...).

Every answer says where it came from (config, guest agent, IPAM, firewall,
storage, cluster), since the same IP can be both a guest address and a
firewall alias.
"""

import asyncio
import ipaddress
import re
import time
from typing import Any

import typer
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    console,
    emit_json,
    parse_tags,
    print_error,
    print_info,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync

_MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)
_NET_KEY = re.compile(r"^net\d+$")
_DISK_KEY = re.compile(r"^(ide|sata|scsi|virtio|efidisk|tpmstate|unused|rootfs|mp)\d*$")
# Guest agent and config reads: one call per guest, capped
_CONCURRENCY = 16


def _parse_size(text: str) -> int:
    """'8G' / '512M' / '1T' / bytes -> bytes."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?", text.strip(), re.IGNORECASE)
    if not m:
        raise typer.BadParameter(f"Invalid size '{text}', use e.g. 8G or 512M")
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def _parse_age(text: str) -> int:
    """'30d' / '12h' / '2w' -> seconds."""
    m = re.fullmatch(r"(\d+)\s*([hdw])", text.strip(), re.IGNORECASE)
    if not m:
        raise typer.BadParameter(f"Invalid age '{text}', use e.g. 12h, 30d or 2w")
    return int(m.group(1)) * {"h": 3600, "d": 86400, "w": 604800}[m.group(2).lower()]


def _kv(value: str) -> dict[str, str]:
    """'virtio=AA:BB,bridge=vmbr0,tag=42' -> dict, first bare word under ''."""
    out: dict[str, str] = {}
    for part in str(value).split(","):
        k, sep, v = part.partition("=")
        if sep:
            out[k.strip()] = v.strip()
        elif "" not in out:
            out[""] = part.strip()
    return out


def _nic_mac(conf: dict[str, str]) -> str | None:
    """MAC of a net line, qemu (virtio=MAC) or lxc (hwaddr=MAC)."""
    if conf.get("hwaddr"):
        return conf["hwaddr"].upper()
    for model in ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci", "pcnet", "i82551", "i82557b", "i82559er"):
        if conf.get(model):
            return conf[model].upper()
    return None


def _classify(query: str) -> tuple[str, Any]:
    """Detect what the query is."""
    q = query.strip()
    try:
        if "/" in q:
            return "net", ipaddress.ip_network(q, strict=False)
        return "ip", ipaddress.ip_address(q)
    except ValueError:
        pass
    if _MAC_RE.match(q):
        return "mac", q.replace("-", ":").upper()
    if q.isdigit():
        return "vmid", int(q)
    if re.search(r"(^|[:/])(vm|base|subvol)-\d+-", q):
        return "volume", q
    return "text", q.lower()


def _ip_matches(value: str, kind: str, target: Any) -> bool:
    try:
        ip = ipaddress.ip_address(str(value).split("/")[0])
    except ValueError:
        return False
    return ip == target if kind == "ip" else ip in target


class _Finder:
    """Collects the cluster facts a search needs, each source at most once."""

    def __init__(self, client: ProxmoxClient) -> None:
        self.client = client
        self.sem = asyncio.Semaphore(_CONCURRENCY)
        self._guests: list[dict] | None = None
        self._configs: dict[int, dict] = {}

    async def _get(self, path: str, **params: Any) -> Any:
        async with self.sem:
            try:
                return await self.client.get(path, params=params or None)
            except PVECliError:
                return None

    async def guests(self) -> list[dict]:
        if self._guests is None:
            self._guests = [
                g for g in await self.client.get_cluster_resources(resource_type="vm")
                if g.get("type") in ("qemu", "lxc")
            ]
        return self._guests

    async def configs(self) -> dict[int, dict]:
        if not self._configs:
            guests = await self.guests()
            results = await asyncio.gather(*(
                self._get(f"/nodes/{g['node']}/{g['type']}/{g['vmid']}/config") for g in guests
            ))
            self._configs = {g["vmid"]: (r or {}) for g, r in zip(guests, results, strict=True)}
        return self._configs

    async def agent_ips(self) -> dict[int, list[tuple[str, str, str]]]:
        """{vmid: [(ip, mac, iface)]} live from the guest agent or LXC interfaces."""
        guests = [g for g in await self.guests() if g.get("status") == "running"]
        configs = await self.configs()

        async def one(g: dict) -> list[tuple[str, str, str]]:
            out = []
            if g["type"] == "qemu":
                if not str(configs.get(g["vmid"], {}).get("agent", "0")).startswith(("1", "enabled=1")):
                    return out
                data = await self._get(f"/nodes/{g['node']}/qemu/{g['vmid']}/agent/network-get-interfaces")
                for iface in (data or {}).get("result", []) or []:
                    for addr in iface.get("ip-addresses", []) or []:
                        out.append((addr.get("ip-address", ""), str(iface.get("hardware-address", "")).upper(), iface.get("name", "")))
            else:
                data = await self._get(f"/nodes/{g['node']}/lxc/{g['vmid']}/interfaces")
                for iface in data or []:
                    # inet/inet6 carry the prefix (10.0.0.5/24); ip-addresses
                    # only exists on recent versions
                    for key in ("inet", "inet6"):
                        if iface.get(key):
                            out.append((iface[key], str(iface.get("hwaddr", "")).upper(), iface.get("name", "")))
            # Bare addresses only: prefixes and IPv6 zone ids break matching
            return [(ip.split("/")[0].split("%")[0], mac, name) for ip, mac, name in out if ip]

        results = await asyncio.gather(*(one(g) for g in guests))
        return {g["vmid"]: r for g, r in zip(guests, results, strict=True)}

    async def ipam(self) -> list[dict]:
        entries: list[dict] = []
        for ipam in await self._get("/cluster/sdn/ipams") or []:
            for e in await self._get(f"/cluster/sdn/ipams/{ipam.get('ipam')}/status") or []:
                entries.append({**e, "_ipam": ipam.get("ipam")})
        return entries


def _row(g: dict, match: str, source: str) -> dict:
    return {
        "type": "VM" if g.get("type") == "qemu" else "CT",
        "vmid": g.get("vmid"),
        "name": g.get("name", ""),
        "node": g.get("node", ""),
        "status": g.get("status", ""),
        "match": match,
        "source": source,
    }


async def _search_query(f: _Finder, query: str) -> list[dict]:
    """Rows matching the free query, guests and infrastructure alike."""
    kind, target = _classify(query)
    rows: list[dict] = []
    guests = {g["vmid"]: g for g in await f.guests()}

    if kind == "text":
        # A resolvable DNS name is searched as its addresses
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(query, None)
            addrs = sorted({i[4][0] for i in infos})
        except (OSError, UnicodeError):
            addrs = []
        if addrs and "." in query:
            for a in addrs:
                rows += [{**r, "match": f"{query} -> {r['match']}"} for r in await _search_query(f, a)]
        for g in guests.values():
            if target in str(g.get("name", "")).lower():
                rows.append(_row(g, f"name {g.get('name')}", "cluster"))
        configs = await f.configs()
        for vmid, cfg in configs.items():
            desc = str(cfg.get("description", ""))
            if target in desc.lower() and vmid in guests:
                rows.append(_row(guests[vmid], "notes: " + desc.strip().splitlines()[0][:60], "config"))
        return rows

    if kind == "vmid":
        g = guests.get(target)
        return [_row(g, f"vmid {target}", "cluster")] if g else []

    if kind == "volume":
        configs = await f.configs()
        for vmid, cfg in configs.items():
            for key, val in cfg.items():
                if _DISK_KEY.match(key) and target in str(val).split(",")[0] and vmid in guests:
                    rows.append(_row(guests[vmid], f"{key} {str(val).split(',')[0]}", "config"))
        if not rows:
            print_info(
                f"No guest uses '{target}': if it still exists on a storage, it is orphaned"
            )
        return rows

    configs = await f.configs()
    if kind == "mac":
        for vmid, cfg in configs.items():
            for key, val in cfg.items():
                if _NET_KEY.match(key) and _nic_mac(_kv(val)) == target and vmid in guests:
                    rows.append(_row(guests[vmid], f"{key} {target}", "config"))
        for e in await f.ipam():
            if str(e.get("mac", "")).upper() == target:
                g = guests.get(e.get("vmid"))
                if g:
                    rows.append(_row(g, f"{e.get('ip')} on {e.get('vnet')}", f"IPAM {e['_ipam']}"))
        return rows

    # IP or network
    for vmid, cfg in configs.items():
        for key, val in cfg.items():
            if re.match(r"^(ipconfig\d+|net\d+)$", key):
                ip = _kv(val).get("ip", "")
                if ip and ip not in ("dhcp", "manual") and _ip_matches(ip, kind, target) and vmid in guests:
                    rows.append(_row(guests[vmid], f"{key} {ip}", "config"))
    for vmid, addrs in (await f.agent_ips()).items():
        for ip, mac, iface in addrs:
            if _ip_matches(ip, kind, target) and not ipaddress.ip_address(ip).is_loopback:
                g = guests[vmid]
                rows.append(_row(g, f"{ip} on {iface} ({mac})", "agent" if g["type"] == "qemu" else "lxc"))
    for e in await f.ipam():
        if _ip_matches(e.get("ip", ""), kind, target):
            g = guests.get(e.get("vmid"))
            if g:
                rows.append(_row(g, f"{e['ip']} on {e.get('vnet')}", f"IPAM {e['_ipam']}"))
            elif e.get("gateway"):
                rows.append(_infra("SDN gateway", e.get("vnet", ""), e["ip"], f"IPAM {e['_ipam']}"))

    # Infrastructure
    for n in await f._get("/cluster/status") or []:
        if n.get("type") == "node" and n.get("ip") and _ip_matches(n["ip"], kind, target):
            rows.append(_infra("node", n.get("name", ""), n["ip"], "cluster"))
    for st in await f._get("/storage") or []:
        hosts = re.split(r"[,; ]+", " ".join(
            str(st.get(k, "")) for k in ("server", "portal", "monhost") if st.get(k)
        ))
        for h in hosts:
            if h and _ip_matches(h.split(":")[0] if h.count(":") == 1 else h, kind, target):
                rows.append(_infra(f"storage ({st.get('type')})", st.get("storage", ""), h, "storage"))
    aliases = await f._get("/cluster/firewall/aliases") or []
    for a in aliases:
        if _ip_in_cidr(a.get("cidr", ""), kind, target):
            rows.append(_infra("firewall alias", a.get("name", ""), a.get("cidr", ""), "firewall"))
    for ipset in await f._get("/cluster/firewall/ipset") or []:
        for entry in await f._get(f"/cluster/firewall/ipset/{ipset.get('name')}") or []:
            if _ip_in_cidr(entry.get("cidr", ""), kind, target):
                rows.append(_infra("firewall ipset", ipset.get("name", ""), entry.get("cidr", ""), "firewall"))
    return rows


def _ip_in_cidr(cidr: str, kind: str, target: Any) -> bool:
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return target in net if kind == "ip" else net.overlaps(target)


def _infra(what: str, name: str, value: str, source: str) -> dict:
    return {"type": what, "vmid": None, "name": name, "node": "", "status": "", "match": value, "source": source}


async def _apply_filters(f: _Finder, rows: list[dict] | None, opts: dict) -> list[dict]:
    """Narrow the rows (or every guest when no query) with the filters."""
    guests = {g["vmid"]: g for g in await f.guests()}
    if rows is None:
        rows = [_row(g, "", "cluster") for g in guests.values()]
    guest_rows = [r for r in rows if r["vmid"] is not None]
    infra_rows = [r for r in rows if r["vmid"] is None]
    if not any(v not in (None, False) for v in opts.values()):
        return rows

    needs_config = any(opts[k] for k in ("on_vnet", "on_bridge", "vlan", "on_storage", "iso", "no_agent", "os"))
    configs = await f.configs() if needs_config else {}
    not_backed = None
    if opts["no_backup"]:
        not_backed = {e.get("vmid") for e in await f._get("/cluster/backup-info/not-backed-up") or []}
    rules: dict[int, list[str]] = {}
    if opts["rule"]:
        for r in await f._get("/cluster/ha/rules") or []:
            for sid in str(r.get("resources", "")).split(","):
                if ":" in sid:
                    rules.setdefault(int(sid.split(":")[1]), []).append(r.get("rule", ""))

    def keep(r: dict) -> str | None:
        """Why the row is kept ('' for no extra note), None to drop it."""
        g = guests.get(r["vmid"], {})
        cfg = configs.get(r["vmid"], {})
        notes: list[str] = []
        nets = [_kv(v) for k, v in cfg.items() if _NET_KEY.match(k)]
        disks = {k: str(v) for k, v in cfg.items() if _DISK_KEY.match(k)}
        if opts["stopped"] and g.get("status") != "stopped":
            return None
        if opts["running"] and g.get("status") != "running":
            return None
        if opts["template"] and not g.get("template"):
            return None
        if opts["locked"]:
            if not g.get("lock"):
                return None
            notes.append(f"locked {g['lock']}")
        if opts["pool"] and g.get("pool") != opts["pool"]:
            return None
        if opts["tag"] and opts["tag"] not in parse_tags(g.get("tags", "") or ""):
            return None
        if opts["no_tags"] and (g.get("tags") or "").strip():
            return None
        if opts["min_ram"] and (g.get("maxmem") or 0) < opts["min_ram"]:
            return None
        if opts["min_cores"] and (g.get("maxcpu") or 0) < opts["min_cores"]:
            return None
        if opts["on_vnet"] or opts["on_bridge"]:
            want = opts["on_vnet"] or opts["on_bridge"]
            if not any(n.get("bridge") == want for n in nets):
                return None
        if opts["vlan"] is not None and not any(n.get("tag") == str(opts["vlan"]) for n in nets):
            return None
        if opts["on_storage"]:
            hit = [k for k, v in disks.items() if v.split(",")[0].split(":")[0] == opts["on_storage"]]
            if not hit:
                return None
            notes.append("disks " + ", ".join(hit))
        if opts["iso"]:
            hit = [v.split(",")[0] for v in disks.values() if "media=cdrom" in v and opts["iso"] in v.split(",")[0]]
            if not hit:
                return None
            notes.append("iso " + hit[0])
        if opts["no_agent"]:
            if g.get("type") != "qemu" or str(cfg.get("agent", "0")).startswith(("1", "enabled=1")):
                return None
        if opts["os"]:
            ostype = str(cfg.get("ostype", ""))
            want = opts["os"].lower()
            match = ostype.startswith("win") if want == "windows" else want in ostype.lower()
            if not match:
                return None
            notes.append(f"ostype {ostype}")
        if not_backed is not None and r["vmid"] not in not_backed:
            return None
        if opts["rule"] and opts["rule"] not in rules.get(r["vmid"], []):
            return None
        return "; ".join(notes)

    kept = []
    for r in guest_rows:
        why = keep(r)
        if why is None:
            continue
        if why:
            r = {**r, "match": "; ".join(x for x in (r["match"], why) if x)}
        kept.append(r)

    if opts["old_snapshots"]:
        limit = time.time() - opts["old_snapshots"]

        async def old(r: dict) -> dict | None:
            g = guests[r["vmid"]]
            snaps = await f._get(f"/nodes/{g['node']}/{g['type']}/{g['vmid']}/snapshot") or []
            stale = [s for s in snaps if s.get("name") != "current" and (s.get("snaptime") or 0) < limit]
            if not stale:
                return None
            names = ", ".join(s["name"] for s in stale[:3]) + (" ..." if len(stale) > 3 else "")
            return {**r, "match": "; ".join(x for x in (r["match"], f"{len(stale)} old snapshot(s): {names}") if x)}

        kept = [r for r in await asyncio.gather(*(old(r) for r in kept)) if r]
    if opts["snapshot"]:
        async def has_snap(r: dict) -> dict | None:
            g = guests[r["vmid"]]
            snaps = await f._get(f"/nodes/{g['node']}/{g['type']}/{g['vmid']}/snapshot") or []
            return r if any(s.get("name") == opts["snapshot"] for s in snaps) else None

        kept = [r for r in await asyncio.gather(*(has_snap(r) for r in kept)) if r]

    # Infrastructure answers only survive when no guest filter applies
    return kept + ([] if any(opts.values()) else infra_rows)


async def _add_tasks(f: _Finder, rows: list[dict], limit: int = 3) -> None:
    """Attach the last actions run on each guest (who, what, when)."""
    tasks = await f._get("/cluster/tasks") or []
    for r in rows:
        if r["vmid"] is None:
            continue
        mine = [t for t in tasks if str(t.get("id", "")) == str(r["vmid"])]
        mine.sort(key=lambda t: t.get("starttime", 0), reverse=True)
        r["tasks"] = [
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(t.get('starttime', 0)))} "
            f"{t.get('type')} by {t.get('user')} ({t.get('status', 'running')})"
            for t in mine[:limit]
        ]


async def _access_rows(f: _Finder, access: int | None, user: str | None) -> list[dict]:
    """ACL entries granting access to a guest, or held by a user or token."""
    acls = await f._get("/access/acl") or []
    rows = []
    if access is not None:
        g = next((x for x in await f.guests() if x.get("vmid") == access), None)
        if not g:
            raise PVECliError(f"Guest {access} not found")
        paths = {"/", "/vms", f"/vms/{access}"}
        if g.get("pool"):
            paths |= {"/pool", f"/pool/{g['pool']}"}
        for a in acls:
            if a.get("path") in paths and (a.get("path") == f"/vms/{access}" or a.get("propagate", 1)):
                rows.append({"who": a.get("ugid"), "kind": a.get("type"), "role": a.get("roleid"),
                             "path": a.get("path"), "propagate": bool(a.get("propagate", 1))})
    if user:
        base = user.split("!")[0]
        info = await f._get(f"/access/users/{base}") or {}
        groups = set(info.get("groups") or [])
        for a in acls:
            if (a.get("type") in ("user", "token") and a.get("ugid") == user) or (
                a.get("type") == "group" and a.get("ugid") in groups and "!" not in user
            ):
                rows.append({"who": a.get("ugid"), "kind": a.get("type"), "role": a.get("roleid"),
                             "path": a.get("path"), "propagate": bool(a.get("propagate", 1))})
    return rows


@fleet(parallel=True)
@async_to_sync
async def find(
    query: str = typer.Argument(None, help="IP, network (10.0.0.0/24), MAC, VMID, disk volume, DNS name or text"),
    on_vnet: str = typer.Option(None, "--on-vnet", help="Guests attached to this SDN vnet"),
    on_bridge: str = typer.Option(None, "--on-bridge", help="Guests attached to this bridge"),
    vlan: int = typer.Option(None, "--vlan", help="Guests using this VLAN tag on a NIC"),
    on_storage: str = typer.Option(None, "--on-storage", help="Guests with a disk on this storage"),
    iso: str = typer.Option(None, "--iso", help="Guests with this ISO (or part of its name) mounted"),
    rule: str = typer.Option(None, "--rule", help="Guests referenced by this HA rule"),
    pool: str = typer.Option(None, "--pool", help="Guests of this resource pool"),
    tag: str = typer.Option(None, "--tag", help="Guests carrying this tag"),
    snapshot: str = typer.Option(None, "--snapshot", help="Guests having a snapshot with this name"),
    stopped: bool = typer.Option(False, "--stopped", is_flag=True, help="Only stopped guests"),
    running: bool = typer.Option(False, "--running", is_flag=True, help="Only running guests"),
    template: bool = typer.Option(False, "--template", is_flag=True, help="Only templates"),
    locked: bool = typer.Option(False, "--locked", is_flag=True, help="Only locked guests"),
    no_agent: bool = typer.Option(False, "--no-agent", is_flag=True, help="VMs without the guest agent enabled"),
    no_backup: bool = typer.Option(False, "--no-backup", is_flag=True, help="Guests no backup job covers"),
    no_tags: bool = typer.Option(False, "--no-tags", is_flag=True, help="Guests without any tag"),
    old_snapshots: str = typer.Option(None, "--old-snapshots", help="Guests with snapshots older than this (30d, 12h, 2w)"),
    min_ram: str = typer.Option(None, "--min-ram", help="At least this much memory (8G, 512M)"),
    min_cores: int = typer.Option(None, "--min-cores", help="At least this many vCPUs"),
    os_type: str = typer.Option(None, "--os", help="windows, or part of the ostype (l26, win11...)"),
    tasks: bool = typer.Option(False, "--tasks", is_flag=True, help="Show the last actions run on each guest"),
    access: int = typer.Option(None, "--access", help="Who has access to this guest (ACL entries)"),
    user: str = typer.Option(None, "--user", help="What this user or token (user@realm!token) has access to"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile(s): name, list, pattern ('client-*') or @group"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Find guests, addresses, disks and their owners.

    Examples:
        pvecli find 10.20.3.14 -p '*'          # who has this IP, on every cluster
        pvecli find bc:24:11:ee:5a:c4          # which guest owns this MAC
        pvecli find vm-105-disk-1              # which guest uses this volume
        pvecli find web01.example.com          # resolved, then searched by IP
        pvecli find --on-storage local-lvm --stopped
        pvecli find --no-backup -p @prod
        pvecli find --old-snapshots 30d
        pvecli find 10.0.3.0/24 --running --tasks
        pvecli find --access 105               # who can act on guest 105
        pvecli find --user alice@pve           # what alice can reach
    """
    opts = {
        "on_vnet": on_vnet, "on_bridge": on_bridge, "vlan": vlan, "on_storage": on_storage,
        "iso": iso, "rule": rule, "pool": pool, "tag": tag, "snapshot": snapshot,
        "stopped": stopped, "running": running, "template": template, "locked": locked,
        "no_agent": no_agent, "no_backup": no_backup, "no_tags": no_tags,
        "old_snapshots": _parse_age(old_snapshots) if old_snapshots else None,
        "min_ram": _parse_size(min_ram) if min_ram else None,
        "min_cores": min_cores, "os": os_type,
    }
    if access is not None or user:
        try:
            async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
                acl_rows = await _access_rows(_Finder(client), access, user)
        except PVECliError as e:
            print_error(str(e))
            raise typer.Exit(1)
        if json_output:
            emit_json(acl_rows)
            return
        if not acl_rows:
            print_info("No ACL entry found")
            return
        table = Table(header_style="bold cyan")
        for col in ("Who", "Kind", "Role", "Path", "Propagate"):
            table.add_column(col)
        for r in acl_rows:
            table.add_row(str(r["who"]), str(r["kind"]), str(r["role"]), str(r["path"]), "yes" if r["propagate"] else "no")
        console.print(table)
        print_info("root@pam and the owner of a token's user also have access outside ACLs")
        return

    if not query and not any(v not in (None, False) for v in opts.values()):
        print_error("Give something to find, or at least one filter (see --help)")
        raise typer.Exit(1)

    try:
        profile_config = ConfigManager().get_profile(profile)
        async with ProxmoxClient(profile_config) as client:
            finder = _Finder(client)
            rows = await _search_query(finder, query) if query else None
            rows = await _apply_filters(finder, rows, opts)
            # One line per (guest, match): the same fact can come twice.
            # Guests first by ID, then infrastructure by kind and name.
            seen, unique = set(), []
            for r in sorted(rows, key=lambda r: (r["vmid"] is None, r["vmid"] or 0, r["type"], r["name"])):
                key = (r["type"], r["vmid"], r["name"], r["match"], r["source"])
                if key not in seen:
                    seen.add(key)
                    unique.append(r)
            if tasks:
                await _add_tasks(finder, unique)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)

    if json_output:
        emit_json(unique)
        return
    if not unique:
        print_info("Nothing found")
        return
    table = Table(header_style="bold cyan", show_lines=bool(tasks))
    for col, kw in (("Type", {}), ("ID", {"justify": "right"}), ("Name", {}), ("Node", {}),
                    ("Status", {}), ("Match", {"overflow": "fold"}), ("Source", {"style": "dim"})):
        table.add_column(col, **kw)
    if tasks:
        table.add_column("Last actions", overflow="fold")
    for r in unique:
        cells = [r["type"], str(r["vmid"] or "-"), r["name"], r["node"] or "-", r["status"] or "-",
                 r["match"] or "-", r["source"]]
        if tasks:
            cells.append("\n".join(r.get("tasks", [])) or "-")
        table.add_row(*cells)
    console.print(table)
