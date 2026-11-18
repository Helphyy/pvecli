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
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import typer
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import console, print_error, print_info, print_success, print_warning
from ..utils.helpers import async_to_sync, ordered_group

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


def _ipconfig(ip: str | None, gw: str | None, ip6: str | None, gw6: str | None) -> str | None:
    """Build an ipconfigN value, validating the addresses on the way.

    PVE's format keeps the families apart (ip/gw IPv4, ip6/gw6 IPv6) and
    wants addresses in CIDR form: anything else is a 400 at creation time,
    after the clone is made. Checked here, before anything exists.
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
        cidr(ip, "--ip", 4, ("dhcp",))
        parts.append(f"ip={ip}")
    if gw:
        address(gw, "--gw", 4)
        parts.append(f"gw={gw}")
    if ip6:
        cidr(ip6, "--ip6", 6, ("dhcp", "auto"))
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


# The volume of a cloud-init drive: <storage>:vm-<id>-cloudinit[.qcow2], or
# <storage>:cloudinit when asked for. A storage merely named "cloudinit-nfs"
# holding a real disk must not match.
_CI_VOLUME = re.compile(r"^[^:,]+:(vm-\d+-)?cloudinit(\.\w+)?$")


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


# ── deploy ───────────────────────────────────────────────────────────────


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
    try:
        ci = _ci_params(user, password_env, ssh_key, ip, gw, ip6, gw6, dns, search_domain, upgrade, 0)
        mem = _parse_memory(memory) if memory else None
        if disk_size and not re.fullmatch(r"\d+[KMGT]", disk_size, re.IGNORECASE):
            raise PVECliError(f"Invalid --disk-size '{disk_size}', use e.g. 20G")
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            src = await _locate(client, template)
            # The config is authoritative: /cluster/resources lags a few
            # seconds behind a conversion to template
            src_cfg = await client.get(f"/nodes/{src['node']}/qemu/{template}/config") or {}
            if not src_cfg.get("template"):
                raise PVECliError(f"VM {template} is not a template: convert it first (pvecli vm template)")
            node = src["node"]
            newid = vmid or int(await client.get_next_vmid())
            linked = not (full or storage)

            upid = await client.clone_vm(
                node, template, newid, name=name, full=not linked, pool=pool, storage=storage,
            )
            console.print(f"[cyan]Cloning template {template} to VM {newid} ({'linked' if linked else 'full'})...[/cyan]")
            await client.wait_for_task(node, upid, timeout=1800)

            hw: dict[str, Any] = {}
            if cores:
                hw["cores"] = cores
            if mem:
                hw["memory"] = mem
            if tags:
                hw["tags"] = ";".join(t.strip() for t in tags.split(",") if t.strip())
            if hw:
                await client.put(f"/nodes/{node}/qemu/{newid}/config", data=hw)
            if disk_size:
                cfg = await client.get(f"/nodes/{node}/qemu/{newid}/config") or {}
                boot = _boot_disk(cfg)
                if not boot:
                    raise PVECliError(f"VM {newid} created, but no disk to resize")
                await client.resize_vm_disk(node, newid, boot, disk_size)
            notes = await _apply(client, node, newid, ci, None)
            for n in notes:
                print_info(n)
            print_success(f"VM {newid} ({name}) created on {node}")

            if start:
                upid = await client.start_vm(node, newid)
                await client.wait_for_task(node, upid, timeout=300)
                print_success(f"VM {newid} started")
                if wait_ip:
                    console.print(f"[cyan]Waiting up to {wait_ip}s for its address from the guest agent...[/cyan]")
                    addr = await _wait_ip(client, node, newid, wait_ip)
                    if addr:
                        print_success(f"VM {newid} is up at {addr}")
                    else:
                        print_warning(
                            "No address from the guest agent (not installed in the image, or still booting). "
                            f"Look for it with: pvecli find {newid}"
                        )
    except (PVECliError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(1)
