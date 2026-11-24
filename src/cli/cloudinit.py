"""Cloud-init for VMs: show and set the settings, deploy from a template.

  pvecli vm cloudinit show 120
  pvecli vm cloudinit set 120 --user debian --ssh-key ~/.ssh/id_ed25519.pub --ip 10.0.0.21/24 --gw 10.0.0.1
  pvecli vm deploy 9000 web01 --ip dhcp --ssh-key ~/.ssh/id_ed25519.pub --cores 2 --memory 2G --disk-size 20G
"""

import asyncio
import ipaddress
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import typer
from rich.markup import escape
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import APIError, PVECliError
from ..config import ConfigManager
from ..utils import console, print_error, print_info, print_success, print_warning
from ..utils.helpers import async_to_sync, ordered_group
from ._shared import bridge_choices, extract_size

app = typer.Typer(
    help="Cloud-init settings of a VM",
    no_args_is_help=True,
    cls=ordered_group(["show", "set"]),
)

_CI_KEYS = ("ciuser", "cipassword", "sshkeys", "nameserver", "searchdomain", "ciupgrade", "citype", "cicustom")
_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
_DRIVE_SLOTS = ("ide2", "ide0", "ide1", "ide3", "sata5", "sata4", "scsi30")


def _parse_memory(text: str) -> int:
    """'2G' / '512M' / '2048' -> MiB."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([MG]?)i?B?", text.strip(), re.IGNORECASE)
    if not m:
        raise typer.BadParameter(f"Invalid memory '{text}', use e.g. 2G or 512M")
    value = float(m.group(1))
    return int(value * 1024) if m.group(2).upper() == "G" else int(value)


def _read_keys(keys: list[str]) -> str:
    """SSH public keys from files (or given inline), one per line."""
    lines: list[str] = []
    for k in keys:
        path = Path(k).expanduser()
        inline = re.match(r"^(ssh-|ecdsa-|sk-)", k.strip())
        if not path.is_file() and not inline and ("/" in k or k.startswith("~") or k.endswith(".pub")):
            # A path, not a key given inline: say the file is missing
            raise PVECliError(f"SSH key file not found: {k}")
        text = path.read_text() if path.is_file() else k
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if not re.match(r"^(ssh-|ecdsa-|sk-)", line):
                    raise PVECliError(f"Not an SSH public key: {line[:40]}...")
                lines.append(line)
    if not lines:
        raise PVECliError("No SSH public key found in --ssh-key")
    return "\n".join(lines)


def _ipconfig(
    ip: str | None, gw: str | None, ip6: str | None, gw6: str | None,
    v4_keywords: tuple[str, ...] = ("dhcp",), v6_keywords: tuple[str, ...] = ("dhcp", "auto"),
) -> str | None:
    """Build an ipconfigN value, validating the addresses on the way.

    PVE's format keeps the families apart (ip/gw IPv4, ip6/gw6 IPv6) and
    wants addresses in CIDR form: anything else is a 400 at creation time,
    after the clone is made. Checked here, before anything exists. The
    keywords differ for a container net0 (manual is allowed there).
    """

    def cidr(value: str, option: str, version: int, keywords: tuple[str, ...]) -> None:
        if value in keywords:
            return
        if "/" not in value:
            raise PVECliError(f"{option} needs a prefix length, e.g. {value}/{24 if version == 4 else 64}")
        if ipaddress.ip_interface(value).version != version:
            raise PVECliError(f"{option} takes an IPv{version} address, got {value}")

    def address(value: str, option: str, version: int) -> None:
        if ipaddress.ip_address(value).version != version:
            raise PVECliError(f"{option} takes an IPv{version} address, got {value}")

    parts = []
    if ip:
        cidr(ip, "--ip", 4, v4_keywords)
        parts.append(f"ip={ip}")
    if gw:
        address(gw, "--gw", 4)
        parts.append(f"gw={gw}")
    if ip6:
        cidr(ip6, "--ip6", 6, v6_keywords)
        parts.append(f"ip6={ip6}")
    if gw6:
        address(gw6, "--gw6", 6)
        parts.append(f"gw6={gw6}")
    return ",".join(parts) or None


def _ci_params(
    user: str | None, password_env: str | None, ssh_key: list[str] | None,
    ip: str | None, gw: str | None, ip6: str | None, gw6: str | None,
    dns: str | None, search_domain: str | None, upgrade: bool | None, iface: int,
) -> dict[str, Any]:
    """Cloud-init settings to send, only those given."""
    params: dict[str, Any] = {}
    if user:
        params["ciuser"] = user
    if password_env:
        pwd = os.environ.get(password_env)
        if not pwd:
            raise PVECliError(f"Environment variable '{password_env}' is empty or unset")
        params["cipassword"] = pwd
    if ssh_key:
        # PVE wants this one value URL-encoded, on top of the form encoding
        params["sshkeys"] = quote(_read_keys(ssh_key), safe="")
    ipcfg = _ipconfig(ip, gw, ip6, gw6)
    if ipcfg:
        params[f"ipconfig{iface}"] = ipcfg
    if dns:
        params["nameserver"] = dns
    if search_domain:
        params["searchdomain"] = search_domain
    if upgrade is not None:
        params["ciupgrade"] = int(upgrade)
    return params


# The volume of a cloud-init drive: <storage>:vm-<id>-cloudinit (block storage),
# <storage>:<id>/vm-<id>-cloudinit.qcow2 (file storage: dir, NFS, CIFS), or
# <storage>:cloudinit when asked for. A storage merely named "cloudinit-nfs"
# holding a real disk must not match.
_CI_VOLUME = re.compile(r"^[^:,]+:(\d+/)?(vm-\d+-)?cloudinit(\.\w+)?$")


def _cloudinit_drive(cfg: dict[str, Any]) -> str | None:
    return next(
        (k for k, v in cfg.items() if _DISK_KEY.match(k) and _CI_VOLUME.match(str(v).split(",")[0])),
        None,
    )


def _boot_storage(cfg: dict[str, Any]) -> str | None:
    """Storage of the first real disk, where the cloud-init drive goes by default."""
    for k in sorted(cfg):
        v = str(cfg[k])
        if _DISK_KEY.match(k) and "media=cdrom" not in v and not _CI_VOLUME.match(v.split(",")[0]) and ":" in v:
            return v.split(":", 1)[0]
    return None


def _is_data_disk(cfg: dict[str, Any], key: str) -> bool:
    value = str(cfg.get(key, ""))
    return bool(_DISK_KEY.match(key)) and "media=cdrom" not in value and not _CI_VOLUME.match(value.split(",")[0])


def _boot_disk(cfg: dict[str, Any]) -> str | None:
    """The disk the VM boots from: first disk of `boot: order=...`, else the
    legacy `bootdisk`, else the first disk by name. Growing a data disk by
    mistake would leave the system disk at its template size."""
    order = re.search(r"order=([^,]+)", str(cfg.get("boot", "")))
    for key in (order.group(1).split(";") if order else []):
        if _is_data_disk(cfg, key):
            return key
    if cfg.get("bootdisk") and _is_data_disk(cfg, str(cfg["bootdisk"])):
        return str(cfg["bootdisk"])
    return next((k for k in sorted(cfg) if _is_data_disk(cfg, k)), None)


async def _locate(client: ProxmoxClient, vmid: int) -> dict:
    for r in await client.get_cluster_resources(resource_type="vm"):
        if r.get("vmid") == vmid and r.get("type") == "qemu":
            return r
    raise PVECliError(f"VM {vmid} not found")


async def _apply(client: ProxmoxClient, node: str, vmid: int, params: dict[str, Any],
                 drive_storage: str | None) -> list[str]:
    """Write cloud-init settings, add the drive if missing, regenerate it."""
    cfg = await client.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
    notes = []
    if not _cloudinit_drive(cfg):
        storage = drive_storage or _boot_storage(cfg)
        if not storage:
            raise PVECliError("No cloud-init drive and no disk to guess its storage: pass --drive-storage")
        slot = next((s for s in _DRIVE_SLOTS if s not in cfg), None)
        if not slot:
            raise PVECliError("No free slot for a cloud-init drive")
        params = {**params, slot: f"{storage}:cloudinit"}
        notes.append(f"cloud-init drive added as {slot} on {storage}")
    if params:
        await client.put(f"/nodes/{node}/qemu/{vmid}/config", data=params)
    # Rebuild the drive now so a running VM gets it at its next boot, and a
    # stopped one has nothing pending
    await client.put(f"/nodes/{node}/qemu/{vmid}/cloudinit")
    return notes


# ── show / set ───────────────────────────────────────────────────────────


@app.command("show")
@async_to_sync
async def cloudinit_show(
    vmid: int = typer.Argument(..., help="VM ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Cloud-init settings of a VM, with the changes not applied yet."""
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            g = await _locate(client, vmid)
            base = f"/nodes/{g['node']}/qemu/{vmid}"
            cfg = await client.get(f"{base}/config") or {}
            try:
                entries = await client.get(f"{base}/cloudinit") or []
            except PVECliError:
                entries = []
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)

    drive = _cloudinit_drive(cfg)
    pending = {e.get("key"): e for e in entries if "pending" in e or e.get("delete")}
    table = Table(title=f"Cloud-init of VM {vmid} ({cfg.get('name') or g.get('name', '')})", header_style="bold cyan")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", overflow="fold")
    table.add_column("Pending", overflow="fold", style="yellow")
    keys = [k for k in cfg if k in _CI_KEYS or k.startswith("ipconfig")]
    keys += [k for k in pending if k not in keys and (k in _CI_KEYS or str(k).startswith("ipconfig"))]
    for k in sorted(keys):
        value = cfg.get(k, "")
        if k == "sshkeys":
            value = f"{len(unquote(str(value)).splitlines())} key(s)" if value else ""
        if k == "cipassword" and value:
            value = "(set, hidden)"
        p = pending.get(k, {})
        pend = "deleted" if p.get("delete") else p.get("pending", "")
        if k == "sshkeys" and pend:
            pend = f"{len(unquote(str(pend)).splitlines())} key(s)"
        table.add_row(k, str(value) or "-", str(pend) if pend else "")
    console.print(table)
    if drive:
        print_info(f"Cloud-init drive: {drive} ({cfg[drive].split(',')[0]})")
    else:
        print_warning("No cloud-init drive: settings are ignored until one is added (cloudinit set adds it)")


@app.command("set")
@async_to_sync
async def cloudinit_set(
    vmid: int = typer.Argument(..., help="VM ID"),
    user: str = typer.Option(None, "--user", help="User created or reconfigured by cloud-init"),
    password_env: str = typer.Option(None, "--password-env", help="Env var holding the user password (keys are better)"),
    ssh_key: list[str] = typer.Option(None, "--ssh-key", help="Public key file or key (repeatable)"),
    ip: str = typer.Option(None, "--ip", help="IPv4 CIDR (10.0.0.21/24) or dhcp"),
    gw: str = typer.Option(None, "--gw", help="IPv4 gateway"),
    ip6: str = typer.Option(None, "--ip6", help="IPv6 CIDR, dhcp or auto"),
    gw6: str = typer.Option(None, "--gw6", help="IPv6 gateway"),
    dns: str = typer.Option(None, "--dns", help="DNS server(s), space separated"),
    search_domain: str = typer.Option(None, "--search-domain", help="DNS search domain"),
    upgrade: bool = typer.Option(None, "--upgrade/--no-upgrade", help="Upgrade packages at first boot"),
    iface: int = typer.Option(0, "--iface", help="NIC the IP settings apply to (net0 = 0)"),
    drive_storage: str = typer.Option(None, "--drive-storage", help="Storage of the cloud-init drive if one must be added"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Change the cloud-init settings of a VM and regenerate its drive.

    Applied at the next boot of the VM (cloud-init runs its network and
    user modules again when the instance data changes).
    """
    try:
        params = _ci_params(user, password_env, ssh_key, ip, gw, ip6, gw6, dns, search_domain, upgrade, iface)
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            g = await _locate(client, vmid)
            if not params and drive_storage is None:
                raise PVECliError("Nothing to change: pass at least one setting (see --help)")
            notes = await _apply(client, g["node"], vmid, params, drive_storage)
    except (PVECliError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(1)
    for n in notes:
        print_info(n)
    shown = ", ".join(k for k in params if k != "cipassword") + (", cipassword" if "cipassword" in params else "")
    print_success(f"Cloud-init of VM {vmid} updated ({shown or 'drive only'})")
    if g.get("status") == "running":
        print_info("The VM runs: the new settings apply at its next reboot")


# ── deploy: shared by vm deploy, ct deploy and pvecli deploy ─────────────
#
# Every deployment goes through the same three steps, whatever asks for it:
#   prepare  (no network): parse sizes, check addresses, read keys and the
#            password from the environment
#   validate (reads only): template, node, storage, pool, ID, all on the
#            cluster, every problem returned at once
#   create   (writes): clone or create, configure, start
# so a file of twenty guests and a single command refuse the same things.

# PVE's dns-name format, for VM names and CT hostnames
_DNS_NAME = re.compile(
    r"^(?=.{1,253}$)[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)
_SIZE = re.compile(r"(\d+(?:\.\d+)?)([KMGT])", re.IGNORECASE)
_CONTENT = {"images": "VM disks", "rootdir": "container disks", "vztmpl": "container templates"}


def size_bytes(text: str) -> int:
    """'20G' -> bytes. PVE sizes are binary: G is GiB."""
    m = _SIZE.fullmatch(text.strip())
    if not m:
        raise PVECliError(f"Invalid size '{text}', use e.g. 20G")
    return int(float(m.group(1)) * 1024 ** ("KMGT".index(m.group(2).upper()) + 1))


@dataclass
class GuestSpec:
    """One guest to deploy, as asked by vm deploy, ct deploy or a file."""

    kind: str  # "vm" or "ct"
    name: str | None
    template: str = ""  # template VM/CT ID, or a vztmpl volume for a container
    profile: str | None = None
    vmid: int | None = None
    node: str | None = None
    storage: str | None = None
    full: bool = False
    cores: int | None = None
    memory: str | None = None
    swap: str | None = None
    disk_size: str | None = None
    tags: list[str] = field(default_factory=list)
    pool: str | None = None
    ip: str | None = None
    gw: str | None = None
    ip6: str | None = None
    gw6: str | None = None
    dns: str | None = None
    search_domain: str | None = None
    ssh_keys: list[str] = field(default_factory=list)
    password_env: str | None = None
    user: str | None = None
    upgrade: bool | None = None
    bridge: str | None = None
    vlan: int | None = None
    unprivileged: bool | None = None
    features: str | None = None
    # A VM installed from an ISO instead of cloned from a template
    iso: str | None = None
    os_type: str | None = None
    virtio_iso: str | None = None
    cpu: str | None = None
    start: bool = True
    wait_ip: int = 0
    label: str = ""  # where it comes from, for messages
    # API values worked out by prepare, the password among them: never shown
    params: dict[str, Any] = field(default_factory=dict, repr=False)
    # What validate learnt about the source: node, config, clone mode
    src: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def what(self) -> str:
        return "VM" if self.kind == "vm" else "CT"


@dataclass
class Outcome:
    vmid: int | None = None
    node: str | None = None
    ip: str | None = None
    error: str | None = None
    # Something exists on the cluster for this guest: after a failure, it
    # is left for the user to look at, never deleted behind their back
    created: bool = False


Say = Callable[[str, str], None]


def console_say(level: str, msg: str) -> None:
    """Progress of a single deployment, straight to the console."""
    if level == "ok":
        print_success(msg)
    elif level == "warn":
        print_warning(msg)
    else:
        console.print(f"[cyan]{escape(msg)}[/cyan]")


class ClusterState:
    """What creations running at once on one cluster must share, even
    through two profiles pointing at it."""

    def __init__(self) -> None:
        self.reserved: set[int] = set()
        self.id_lock = asyncio.Lock()
        self.clone_locks: dict[int, asyncio.Lock] = {}


class Facts:
    """What a deployment reads from one cluster, read once per run."""

    def __init__(self, client: ProxmoxClient, profile: str | None, state: ClusterState | None = None) -> None:
        self.client = client
        self.profile = profile
        self.state = state or ClusterState()
        self._cache: dict[Any, Any] = {}

    async def _cached(self, key: Any, fetch: Callable[[], Awaitable[Any]]) -> Any:
        if key not in self._cache:
            self._cache[key] = await fetch()
        return self._cache[key]

    async def guests(self) -> list[dict[str, Any]]:
        return await self._cached("guests", lambda: self.client.get_cluster_resources(resource_type="vm")) or []

    async def nodes(self) -> dict[str, str]:
        rows = await self._cached("nodes", self.client.get_nodes) or []
        return {n.get("node", ""): n.get("status", "") for n in rows}

    async def storages(self, node: str) -> dict[str, dict[str, Any]]:
        rows = await self._cached(("storage", node), lambda: self.client.get_storage_list(node)) or []
        return {s.get("storage", ""): s for s in rows}

    async def bridges(self, node: str) -> set[str]:
        rows = await self._cached(("net", node), lambda: self.client.get_network_interfaces(node)) or []
        return set(bridge_choices(rows)[0])

    async def pools(self) -> set[str]:
        rows = await self._cached("pools", self.client.get_pools) or []
        return {p.get("poolid", "") for p in rows}

    async def volumes(self, node: str, storage: str, content: str) -> set[str]:
        rows = await self._cached(
            ("content", node, storage, content), lambda: self.client.get_storage_content(node, storage, content)
        ) or []
        return {v.get("volid", "") for v in rows}

    async def config(self, node: str, kind: str, vmid: int) -> dict[str, Any]:
        return await self._cached(
            ("config", node, kind, vmid), lambda: self.client.get(f"/nodes/{node}/{kind}/{vmid}/config")
        ) or {}

    async def id_free(self, vmid: int) -> bool:
        """Asked to PVE itself: /cluster/resources lags behind a creation."""
        try:
            await self.client.get("/cluster/nextid", params={"vmid": vmid})
            return True
        except APIError as e:
            if e.status_code == 400:
                return False
            raise

    def claim(self, vmid: int) -> None:
        self.state.reserved.add(vmid)

    async def next_id(self) -> int:
        """A free ID that no other creation of this run holds: /cluster/nextid
        hands the same one out until a guest really uses it."""
        async with self.state.id_lock:
            vmid = int(await self.client.get_next_vmid())
            fresh = True  # straight from /cluster/nextid: free by definition
            while vmid in self.state.reserved or not (fresh or await self.id_free(vmid)):
                vmid += 1
                fresh = False
            self.state.reserved.add(vmid)
            return vmid

    def clone_lock(self, vmid: int) -> asyncio.Lock:
        return self.state.clone_locks.setdefault(vmid, asyncio.Lock())


# ── checks shared by VMs and containers ──


def check_name(name: str | None, what: str) -> list[str]:
    if name and not _DNS_NAME.match(name):
        return [f"Invalid {what} '{name}': letters, digits, dots and dashes only"]
    return []


def check_dns(dns: str | None) -> list[str]:
    errors = []
    for server in (dns or "").replace(",", " ").split():
        try:
            ipaddress.ip_address(server)
        except ValueError:
            errors.append(f"Invalid DNS server '{server}': an IP address is expected")
    return errors


def read_password(env: str | None, min_length: int = 0) -> str | None:
    """The password from the environment; never from a file nor an option,
    and never echoed in an error."""
    if not env:
        return None
    pwd = os.environ.get(env)
    if not pwd:
        raise PVECliError(f"Environment variable '{env}' is empty or unset")
    if len(pwd) < min_length:
        raise PVECliError(f"The password in '{env}' is too short: PVE wants at least {min_length} characters")
    return pwd


def attempt(errors: list[str], fn: Callable[[], Any]) -> Any:
    """Run one check, keep its error with the others instead of stopping."""
    try:
        return fn()
    except (PVECliError, ValueError, typer.BadParameter) as e:
        errors.append(e.message if isinstance(e, typer.BadParameter) else str(e))
        return None


async def check_node(facts: Facts, node: str) -> str | None:
    nodes = await facts.nodes()
    if node not in nodes:
        return f"Node '{node}' not found (nodes: {', '.join(sorted(nodes))})"
    if nodes[node] != "online":
        return f"Node '{node}' is {nodes[node] or 'not online'}"
    return None


async def check_storage(facts: Facts, node: str, storage: str, content: str, pve_default: bool = False) -> str | None:
    st = (await facts.storages(node)).get(storage)
    label = f"'{storage}'" + (" (PVE's default, pass --storage)" if pve_default else "")
    if not st:
        return f"Storage {label} not found on {node}"
    if content not in str(st.get("content", "")).split(","):
        return f"Storage {label} on {node} holds no {_CONTENT[content]} (content {content})"
    if not st.get("enabled", 1) or not st.get("active", 1):
        return f"Storage '{storage}' is not active on {node}"
    return None


async def check_common(facts: Facts, spec: GuestSpec) -> list[str]:
    errors = []
    if spec.pool and spec.pool not in await facts.pools():
        errors.append(f"Pool '{spec.pool}' not found")
    if spec.vmid is not None and not await facts.id_free(spec.vmid):
        errors.append(f"ID {spec.vmid} is already taken")
    return errors


# ── VM ──


def prepare_vm(spec: GuestSpec) -> list[str]:
    """Check what needs no cluster and work out the API values."""
    if spec.iso:
        return _prepare_iso_vm(spec)
    errors = check_name(spec.name, "name") + check_dns(spec.dns)
    if spec.os_type or spec.virtio_iso:
        errors.append("os_type and virtio_iso apply to a VM installed from an ISO (iso:), not to a clone")
    if not spec.template.isdigit():
        errors.append(f"A VM template is given by its ID, got '{spec.template}'")
    attempt(errors, lambda: _ipconfig(spec.ip, spec.gw, spec.ip6, spec.gw6))
    if spec.ssh_keys:
        attempt(errors, lambda: _read_keys(spec.ssh_keys))
    attempt(errors, lambda: read_password(spec.password_env))
    mem = attempt(errors, lambda: _parse_memory(str(spec.memory))) if spec.memory else None
    if spec.disk_size and not re.fullmatch(r"\d+[KMGT]", spec.disk_size, re.IGNORECASE):
        errors.append(f"Invalid --disk-size '{spec.disk_size}', use e.g. 20G")
    if spec.vlan is not None and not 1 <= spec.vlan <= 4094:
        errors.append(f"Invalid --vlan {spec.vlan}: 1 to 4094")
    if not errors:
        spec.params = {
            "memory": mem,
            "ci": _ci_params(spec.user, spec.password_env, spec.ssh_keys or None, spec.ip, spec.gw, spec.ip6,
                             spec.gw6, spec.dns, spec.search_domain, spec.upgrade, 0),
        }
    return errors


async def validate_vm(facts: Facts, spec: GuestSpec) -> list[str]:
    """Everything the cluster must offer before the clone starts."""
    if spec.iso:
        return await _validate_iso_vm(facts, spec)
    tid = int(spec.template)
    src = next((g for g in await facts.guests() if g.get("vmid") == tid and g.get("type") == "qemu"), None)
    if not src:
        return [f"VM {tid} not found"]
    node = src["node"]
    # The config is authoritative: /cluster/resources lags a few seconds
    # behind a conversion to template
    cfg = await facts.config(node, "qemu", tid)
    errors = []
    if not cfg.get("template"):
        errors.append(f"VM {tid} is not a template: convert it first (pvecli vm template)")
    target = spec.node or node
    if target != node:
        err = await check_node(facts, target)
        if err:
            return errors + [err]
        disk_storage = _boot_storage(cfg)
        if not (await facts.storages(node)).get(disk_storage or "", {}).get("shared"):
            errors.append(f"Template {tid} is on '{disk_storage}', local to {node}: clone it there")
    if spec.storage:
        err = await check_storage(facts, target, spec.storage, "images")
        if err:
            errors.append(err)
    boot = _boot_disk(cfg)
    current = extract_size(str(cfg.get(boot, ""))) if boot else ""
    if spec.disk_size and current and size_bytes(spec.disk_size) < size_bytes(current):
        errors.append(f"--disk-size {spec.disk_size} is smaller than the template disk ({current}): a disk can only grow")
    if spec.bridge and spec.bridge not in await facts.bridges(target):
        errors.append(f"Bridge '{spec.bridge}' not found on {target}")
    if (spec.bridge or spec.vlan is not None) and not str(cfg.get("net0", "")):
        errors.append(f"Template {tid} has no net0 to put on a bridge or a VLAN")
    errors += await check_common(facts, spec)
    spec.src = {"node": node, "target": target}
    return errors


async def create_vm(facts: Facts, spec: GuestSpec, say: Say, out: Outcome) -> None:
    """Clone, size, cloud-init, start. Raises on the first failure; `out`
    tells what exists by then."""
    if spec.iso:
        await _create_iso_vm(facts, spec, say, out)
        return
    client = facts.client
    tid, node, target = int(spec.template), spec.src["node"], spec.src["target"]
    newid = spec.vmid or await facts.next_id()
    spec.vmid = out.vmid = newid
    out.node = target
    linked = not (spec.full or spec.storage)

    upid = await client.clone_vm(
        node, tid, newid, name=spec.name, full=not linked, pool=spec.pool, storage=spec.storage,
        target=target if target != node else None,
    )
    out.created = True
    say("step", f"Cloning template {tid} to VM {newid} ({'linked' if linked else 'full'})...")
    await client.wait_for_task(node, upid, timeout=1800)

    hw: dict[str, Any] = {}
    if spec.cores:
        hw["cores"] = spec.cores
    if spec.params.get("memory"):
        hw["memory"] = spec.params["memory"]
    if spec.tags:
        hw["tags"] = ";".join(spec.tags)
    if spec.cpu:
        hw["cpu"] = spec.cpu
    if spec.bridge or spec.vlan is not None:
        cfg = await client.get(f"/nodes/{target}/qemu/{newid}/config") or {}
        if not cfg.get("net0"):
            raise PVECliError(f"VM {newid} created, but it has no net0 to put on a bridge or a VLAN")
        hw["net0"] = _net_on(str(cfg["net0"]), spec.bridge, spec.vlan)
    if hw:
        await client.put(f"/nodes/{target}/qemu/{newid}/config", data=hw)
    if spec.disk_size:
        cfg = await client.get(f"/nodes/{target}/qemu/{newid}/config") or {}
        boot = _boot_disk(cfg)
        if not boot:
            raise PVECliError(f"VM {newid} created, but no disk to resize")
        await client.resize_vm_disk(target, newid, boot, spec.disk_size)
    for note in await _apply(client, target, newid, spec.params.get("ci", {}), None):
        say("info", note)
    say("ok", f"VM {newid} ({spec.name}) created on {target}")
    await _start(client, spec, target, newid, say, out)


async def _start(client: ProxmoxClient, spec: GuestSpec, target: str, newid: int, say: Say, out: Outcome) -> None:
    if spec.start:
        upid = await client.start_vm(target, newid)
        await client.wait_for_task(target, upid, timeout=300)
        say("ok", f"VM {newid} started")
        if spec.wait_ip:
            say("step", f"Waiting up to {spec.wait_ip}s for its address from the guest agent...")
            out.ip = await _wait_ip(client, target, newid, spec.wait_ip)
            if out.ip:
                say("ok", f"VM {newid} is up at {out.ip}")
            else:
                say("warn", "No address from the guest agent (not installed in the image, or still booting). "
                            f"Look for it with: pvecli find {newid}")


def _net_on(net: str, bridge: str | None, vlan: int | None) -> str:
    """net0 of a clone on another bridge or VLAN: its model and MAC kept."""
    parts = [p for p in net.split(",") if p and not (bridge and p.startswith("bridge="))
             and not (vlan is not None and p.startswith("tag="))]
    if bridge:
        parts.append(f"bridge={bridge}")
    if vlan is not None:
        parts.append(f"tag={vlan}")
    return ",".join(parts)


# ── VM from an ISO ──

# A VM installed from an ISO: what cloud-init would set means nothing to it
CLOUD_INIT_KEYS = ("user", "upgrade", "ssh_key", "password_env", "ip", "gw", "ip6", "gw6", "dns", "search_domain")
_ISO = re.compile(r"^[\w.-]+:iso/[^/]+$")
_OSTYPES = {"l26", "l24", "win11", "win10", "win8", "win7", "w2k8", "wvista", "wxp", "w2k", "solaris", "other"}
_FILE_STORAGES = {"dir", "nfs", "cifs", "glusterfs"}


def _ostype(os_type: str | None) -> str | None:
    """linux, windows, or an ostype of Proxmox VE as is (None: not one)."""
    wanted = (os_type or "linux").strip().lower()
    return {"linux": "l26", "windows": "win11"}.get(wanted, wanted if wanted in _OSTYPES else None)


def _prepare_iso_vm(spec: GuestSpec) -> list[str]:
    errors = check_name(spec.name, "name")
    used = [k for k, v in (("user", spec.user), ("upgrade", spec.upgrade), ("ssh_key", spec.ssh_keys),
                           ("password_env", spec.password_env), ("ip", spec.ip), ("gw", spec.gw),
                           ("ip6", spec.ip6), ("gw6", spec.gw6), ("dns", spec.dns),
                           ("search_domain", spec.search_domain)) if v not in (None, [], "")]
    if used:
        errors.append(f"An ISO install has no cloud-init: {', '.join(used)} cannot apply (configure the "
                      "guest in its installer, or use a cloud-init template)")
    if spec.full:
        errors.append("full applies to a clone, not to a VM installed from an ISO")
    for key, vol in (("iso", spec.iso), ("virtio_iso", spec.virtio_iso)):
        if vol and not _ISO.match(vol):
            errors.append(f"{key} '{vol}' is not an ISO volume: use STORAGE:iso/FILE.iso")
    ostype = _ostype(spec.os_type)
    if ostype is None:
        errors.append(f"os_type '{spec.os_type}': linux, windows, or one of {', '.join(sorted(_OSTYPES))}")
    if spec.virtio_iso and ostype and not ostype.startswith("w"):
        errors.append("virtio_iso is for a Windows install (os_type: windows)")
    if not spec.storage:
        errors.append("An ISO install needs storage: for its disk")
    if not spec.disk_size:
        errors.append("An ISO install needs disk_size: (e.g. 32G)")
    elif not re.fullmatch(r"\d+G", spec.disk_size, re.IGNORECASE):
        errors.append(f"Invalid disk_size '{spec.disk_size}' for a new disk: whole GiB, e.g. 32G")
    if spec.vlan is not None and not 1 <= spec.vlan <= 4094:
        errors.append(f"Invalid vlan {spec.vlan}: 1 to 4094")
    mem = attempt(errors, lambda: _parse_memory(str(spec.memory))) if spec.memory else 2048
    if not errors:
        spec.params = {"memory": mem, "ostype": ostype}
    return errors


async def _validate_iso_vm(facts: Facts, spec: GuestSpec) -> list[str]:
    """The node, the ISOs, the storage and the bridge, before anything is created."""
    node = spec.node
    if not node:
        online = [n for n, status in (await facts.nodes()).items() if status == "online"]
        if len(online) != 1:
            return ["An ISO install is created on one node: set node:"]
        node = online[0]
    err = await check_node(facts, node)
    if err:
        return [err]
    errors = []
    for vol in (spec.iso, spec.virtio_iso):
        if not vol:
            continue
        store = vol.split(":", 1)[0]
        err = await check_storage(facts, node, store, "iso")
        if err:
            errors.append(err)
        elif vol not in await facts.volumes(node, store, "iso"):
            errors.append(f"ISO '{vol}' not found on {node} (pvecli storage content list {node} {store})")
    assert spec.storage
    err = await check_storage(facts, node, spec.storage, "images")
    if err:
        errors.append(err)
    bridge = spec.bridge or "vmbr0"
    if bridge not in await facts.bridges(node):
        errors.append(f"Bridge '{bridge}' not found on {node}" + ("" if spec.bridge else " (the default): set bridge:"))
    errors += await check_common(facts, spec)
    stype = (await facts.storages(node)).get(spec.storage, {}).get("type")
    spec.src = {"node": node, "target": node, "qcow2": stype in _FILE_STORAGES, "bridge": bridge}
    return errors


def iso_config(spec: GuestSpec) -> dict[str, Any]:
    """The API config of a VM installed from an ISO.

    The empty disk boots first: on the first start it is skipped and the
    ISO starts, once installed the system on it does. No eject is needed.
    """
    ostype = spec.params["ostype"]
    size = spec.disk_size[:-1] if spec.disk_size else "32"
    disk = f"{spec.storage}:{size}" + (",format=qcow2" if spec.src.get("qcow2") else "")
    net = f"virtio,bridge={spec.src['bridge']}" + (f",tag={spec.vlan}" if spec.vlan is not None else "")
    cfg: dict[str, Any] = {
        "name": spec.name, "ostype": ostype, "agent": 1, "sockets": 1, "cores": spec.cores or 2,
        "cpu": spec.cpu or "x86-64-v2-AES", "memory": spec.params["memory"],
        "scsihw": "virtio-scsi-single", "scsi0": disk, "ide2": f"{spec.iso},media=cdrom",
        "net0": net, "boot": "order=scsi0;ide2;net0",
    }
    if ostype.startswith("w"):
        if spec.virtio_iso:
            cfg["ide0"] = f"{spec.virtio_iso},media=cdrom"
        if ostype == "win11":
            # Windows 11 and Server 2022/2025 want UEFI with secure boot and a TPM
            cfg.update(bios="ovmf", machine="q35", efidisk0=f"{spec.storage}:1,efitype=4m,pre-enrolled-keys=1",
                       tpmstate0=f"{spec.storage}:1,version=v2.0")
    if spec.pool:
        cfg["pool"] = spec.pool
    if spec.tags:
        cfg["tags"] = ";".join(spec.tags)
    return cfg


async def _create_iso_vm(facts: Facts, spec: GuestSpec, say: Say, out: Outcome) -> None:
    client = facts.client
    node = spec.src["node"]
    newid = spec.vmid or await facts.next_id()
    spec.vmid = out.vmid = newid
    out.node = node
    upid = await client.create_vm(node, newid, **iso_config(spec))
    out.created = True
    say("step", f"Creating VM {newid} from {spec.iso.rsplit('/', 1)[-1] if spec.iso else ''}...")
    await client.wait_for_task(node, upid, timeout=600)
    say("ok", f"VM {newid} ({spec.name}) created on {node}, its installer on the CD")
    await _start(client, spec, node, newid, say, out)


def split_tags(tags: str | None) -> list[str]:
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def fail(errors: list[str]) -> None:
    for e in errors:
        print_error(e)
    raise typer.Exit(1)


# ── vm deploy ────────────────────────────────────────────────────────────


async def _wait_ip(client: ProxmoxClient, node: str, vmid: int, timeout: int) -> str | None:
    """First non-local IPv4 reported by the guest agent, or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = await client.get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}
            for iface in data.get("result", []) or []:
                for addr in iface.get("ip-addresses", []) or []:
                    ip = addr.get("ip-address", "")
                    if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                        return ip
        except PVECliError:
            pass
        await asyncio.sleep(5)
    return None


@async_to_sync
async def deploy(
    template: int = typer.Argument(..., help="Template VM ID to clone"),
    name: str = typer.Argument(..., help="Name of the new VM"),
    vmid: int = typer.Option(None, "--vmid", help="ID of the new VM (default: next free)"),
    full: bool = typer.Option(False, "--full", help="Full clone instead of a linked clone"),
    storage: str = typer.Option(None, "--storage", help="Target storage (implies --full)"),
    cores: int = typer.Option(None, "--cores", help="vCPUs"),
    memory: str = typer.Option(None, "--memory", help="Memory: 2G, 4096M..."),
    disk_size: str = typer.Option(None, "--disk-size", help="Grow the boot disk to this size (20G)"),
    tags: str = typer.Option(None, "--tags", help="Tags, comma separated"),
    pool: str = typer.Option(None, "--pool", help="Resource pool"),
    user: str = typer.Option(None, "--user", help="Cloud-init user"),
    password_env: str = typer.Option(None, "--password-env", help="Env var holding the user password"),
    ssh_key: list[str] = typer.Option(None, "--ssh-key", help="Public key file or key (repeatable)"),
    ip: str = typer.Option(None, "--ip", help="IPv4 CIDR (10.0.0.21/24) or dhcp"),
    gw: str = typer.Option(None, "--gw", help="IPv4 gateway"),
    ip6: str = typer.Option(None, "--ip6", help="IPv6 CIDR, dhcp or auto"),
    gw6: str = typer.Option(None, "--gw6", help="IPv6 gateway"),
    dns: str = typer.Option(None, "--dns", help="DNS server(s)"),
    search_domain: str = typer.Option(None, "--search-domain", help="DNS search domain"),
    upgrade: bool = typer.Option(None, "--upgrade/--no-upgrade", help="Upgrade packages at first boot"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start the VM once configured"),
    wait_ip: int = typer.Option(0, "--wait-ip", help="Seconds to wait for its IP from the guest agent (0: don't)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Create a ready-to-use VM from a cloud-init template in one command.

    Clone, size (CPU, memory, disk), cloud-init (user, keys, network), tags
    and pool, then start. Every value is checked before anything is created.

    Examples:
        pvecli vm deploy 9000 web01 --ip dhcp --ssh-key ~/.ssh/id_ed25519.pub
        pvecli vm deploy 9000 db01 --ip 10.0.0.31/24 --gw 10.0.0.1 --dns 10.0.0.1 \\
            --cores 4 --memory 8G --disk-size 50G --tags db,prod --wait-ip 120
    """
    spec = GuestSpec(
        kind="vm", name=name, template=str(template), profile=profile, vmid=vmid, storage=storage, full=full,
        cores=cores, memory=memory, disk_size=disk_size, tags=split_tags(tags), pool=pool, user=user,
        password_env=password_env, ssh_keys=list(ssh_key or []), ip=ip, gw=gw, ip6=ip6, gw6=gw6, dns=dns,
        search_domain=search_domain, upgrade=upgrade, start=start, wait_ip=wait_ip,
    )
    errors = prepare_vm(spec)
    if errors:
        fail(errors)
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            facts = Facts(client, profile)
            errors = await validate_vm(facts, spec)
            if errors:
                fail(errors)
            await create_vm(facts, spec, console_say, Outcome())
    except (PVECliError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(1)
