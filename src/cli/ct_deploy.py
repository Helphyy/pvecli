"""Deploy a container in one command, from a file template or a CT template.

  pvecli ct deploy local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst --hostname web01 \\
      --node pve1 --storage local-lvm --ip 10.0.0.21/24 --gw 10.0.0.1 --ssh-key ~/.ssh/id_ed25519.pub
  pvecli ct deploy 920 --hostname web02 --ip dhcp --cores 2 --memory 1G --disk-size 8G

A file template goes through POST /nodes/{node}/lxc; a CT template is cloned
(linked when its storage allows it), then configured with PUT config. PVE
only takes a root password and SSH keys at creation: a clone keeps those of
its template.
"""

import asyncio
import re
import shlex
import time
from typing import Any

import typer
from rich.markup import escape

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import console
from ..utils.helpers import async_to_sync
from ._shared import build_kv, extract_size, parse_kv
from .cloudinit import (
    Facts,
    GuestSpec,
    Outcome,
    Say,
    _ipconfig,
    _parse_memory,
    _read_keys,
    attempt,
    check_common,
    check_dns,
    check_name,
    check_node,
    check_storage,
    console_say,
    fail,
    read_password,
    size_bytes,
    split_tags,
)
from .ct import _LINKED_CLONE_STORAGE_TYPES, _template_clone_full, app

_FEATURES = ("force_rw_sys", "fuse", "keyctl", "mknod", "mount", "nesting")
# Bridge of a NIC pvecli has to create (PVE has no default for it)
DEFAULT_BRIDGE = "vmbr0"
# Where PVE puts the root disk of a new container when not told (POST /lxc 'storage')
PVE_DEFAULT_STORAGE = "local"
# PVE refuses a shorter root password (POST /lxc 'password' minLength)
_MIN_PASSWORD = 5


def is_file_template(template: str) -> bool:
    return not template.isdigit()


def rootfs_gib(size: str) -> int:
    """'8G' -> 8: the root disk of a new container is given as <storage>:<GiB>."""
    if not re.fullmatch(r"\d+(?:\.\d+)?[MGT]", size.strip(), re.IGNORECASE):
        raise PVECliError(f"Invalid disk size '{size}', use e.g. 8G")
    gib = size_bytes(size) / 1024**3
    if gib < 1 or gib != int(gib):
        raise PVECliError(f"Disk size '{size}' must be a whole number of GiB, e.g. 8G")
    return int(gib)


def _features(text: str) -> str:
    for part in text.split(","):
        key = part.split("=", 1)[0].strip()
        if key not in _FEATURES:
            raise PVECliError(f"Unknown container feature '{key}' (known: {', '.join(_FEATURES)})")
    return text


def _net_wanted(spec: GuestSpec) -> dict[str, Any]:
    pairs = (("bridge", spec.bridge), ("ip", spec.ip), ("gw", spec.gw), ("ip6", spec.ip6), ("gw6", spec.gw6),
             ("tag", spec.vlan))
    return {k: v for k, v in pairs if v is not None}


def lxc_net0(current: str | None, spec: GuestSpec) -> str | None:
    """The net0 value to send: a new NIC when there is none, else the
    current one with only the asked keys changed (its MAC stays). None when
    there is nothing to change."""
    wanted = _net_wanted(spec)
    if current is None:
        return build_kv({"name": "eth0", "bridge": DEFAULT_BRIDGE, **wanted})
    if not wanted:
        return None
    kv = parse_kv(current)
    kv.update({k: str(v) for k, v in wanted.items()})
    return build_kv(kv)


def prepare_ct(spec: GuestSpec) -> list[str]:
    """Check what needs no cluster and work out the API values."""
    errors = check_name(spec.name, "hostname") + check_dns(spec.dns)
    if is_file_template(spec.template):
        if ":" not in spec.template:
            errors.append(f"Give the template as a volume, e.g. local:vztmpl/{spec.template}, or a CT template ID")
        elif ":vztmpl/" not in spec.template:
            store, rest = spec.template.split(":", 1)
            spec.template = f"{store}:vztmpl/{rest}"
    attempt(errors, lambda: _ipconfig(spec.ip, spec.gw, spec.ip6, spec.gw6,
                                      ("dhcp", "manual"), ("dhcp", "auto", "manual")))
    if spec.vlan is not None and not 1 <= spec.vlan <= 4094:
        errors.append(f"VLAN tag {spec.vlan} is out of range: 1 to 4094")
    params: dict[str, Any] = {}
    if spec.memory:
        params["memory"] = attempt(errors, lambda: _parse_memory(str(spec.memory)))
    if spec.swap is not None:
        params["swap"] = attempt(errors, lambda: _parse_memory(str(spec.swap)))
    if spec.disk_size:
        if is_file_template(spec.template):
            params["rootfs_gib"] = attempt(errors, lambda: rootfs_gib(str(spec.disk_size)))
        else:
            attempt(errors, lambda: size_bytes(str(spec.disk_size)))
    if spec.features:
        attempt(errors, lambda: _features(str(spec.features)))
    if spec.ssh_keys:
        params["ssh-public-keys"] = attempt(errors, lambda: _read_keys(spec.ssh_keys))
    if spec.password_env:
        params["password"] = attempt(errors, lambda: read_password(spec.password_env, _MIN_PASSWORD))
    spec.params = params
    return errors


async def validate_ct(facts: Facts, spec: GuestSpec) -> list[str]:
    """Everything the cluster must offer before anything is created."""
    if is_file_template(spec.template):
        errors = await _validate_create(facts, spec)
    else:
        errors = await _validate_clone(facts, spec)
    return errors + await check_common(facts, spec)


async def _check_bridge(facts: Facts, node: str, bridge: str) -> list[str]:
    if bridge not in await facts.bridges(node):
        return [f"Bridge '{bridge}' not found on {node}" + ("" if bridge != DEFAULT_BRIDGE else " (pass --bridge)")]
    return []


async def _validate_create(facts: Facts, spec: GuestSpec) -> list[str]:
    if spec.node:
        err = await check_node(facts, spec.node)
        if err:
            return [err]
        node = spec.node
    else:
        # The template file usually sits on a node-local storage: no guess
        online = sorted(n for n, status in (await facts.nodes()).items() if status == "online")
        if len(online) != 1:
            return [f"{len(online)} nodes are online: pass --node"]
        node = online[0]
    errors = []
    store = spec.template.split(":", 1)[0]
    err = await check_storage(facts, node, store, "vztmpl")
    if err:
        errors.append(err)
    elif spec.template not in await facts.volumes(node, store, "vztmpl"):
        errors.append(f"Template {spec.template} not found on {node}")
    err = await check_storage(facts, node, spec.storage or PVE_DEFAULT_STORAGE, "rootdir", pve_default=not spec.storage)
    if err:
        errors.append(err)
    errors += await _check_bridge(facts, node, spec.bridge or DEFAULT_BRIDGE)
    spec.src = {"mode": "create", "node": node, "target": node}
    return errors


async def _validate_clone(facts: Facts, spec: GuestSpec) -> list[str]:
    tid = int(spec.template)
    src = next((g for g in await facts.guests() if g.get("vmid") == tid), None)
    if not src or src.get("type") != "lxc":
        return [f"CT template {tid} not found" + (" (it is a VM)" if src else "")]
    node = src["node"]
    # The config is authoritative: /cluster/resources lags behind a conversion
    cfg = await facts.config(node, "lxc", tid)
    errors = []
    if not cfg.get("template"):
        errors.append(f"CT {tid} is not a template: convert it first (pvecli ct template)")
    # POST /lxc/{id}/clone and PUT config take neither: only a creation does
    if spec.ssh_keys:
        errors.append("SSH keys (--ssh-key) are only taken when creating from a file template: "
                      "a clone keeps those of its CT template")
    if spec.password_env:
        errors.append("A root password (--password-env) is only taken when creating from a file template: "
                      "a clone keeps the one of its CT template")
    unprivileged = str(cfg.get("unprivileged", 0)) == "1"
    if spec.unprivileged is not None and spec.unprivileged != unprivileged:
        errors.append(f"CT template {tid} is {'unprivileged' if unprivileged else 'privileged'}: "
                      "a clone keeps it, PVE cannot change it")
    rootfs = str(cfg.get("rootfs", ""))
    src_store, src_size = rootfs.split(":", 1)[0], extract_size(rootfs)
    stores = await facts.storages(node)
    target = spec.node or node
    if target != node:
        err = await check_node(facts, target)
        if err:
            return errors + [err]
        if not stores.get(src_store, {}).get("shared"):
            errors.append(f"CT template {tid} is on '{src_store}', local to {node}: it can only be cloned there")
    if spec.storage:
        err = await check_storage(facts, target, spec.storage, "rootdir")
        if err:
            errors.append(err)
    if spec.disk_size and src_size and size_bytes(spec.disk_size) < size_bytes(src_size):
        errors.append(f"--disk-size {spec.disk_size} is smaller than the template disk ({src_size}): "
                      "a disk can only grow")
    if spec.bridge or (_net_wanted(spec) and not cfg.get("net0")):
        errors += await _check_bridge(facts, target, spec.bridge or DEFAULT_BRIDGE)
    linked_ok = stores.get(src_store, {}).get("type") in _LINKED_CLONE_STORAGE_TYPES
    spec.src = {"mode": "clone", "node": node, "target": target,
                "full": _template_clone_full(spec.full or bool(spec.storage), linked_ok)}
    return errors


def _create_data(spec: GuestSpec, vmid: int) -> dict[str, Any]:
    p = spec.params
    data: dict[str, Any] = {"vmid": vmid, "ostemplate": spec.template}
    if spec.name:
        data["hostname"] = spec.name
    # PVE creates unprivileged by default, but its schema says 0: sent
    # explicitly so the result never depends on which one is right
    data["unprivileged"] = 0 if spec.unprivileged is False else 1
    if p.get("rootfs_gib"):
        data["rootfs"] = f"{spec.storage or PVE_DEFAULT_STORAGE}:{p['rootfs_gib']}"
    elif spec.storage:
        data["storage"] = spec.storage
    data.update(_hardware(spec))
    data["net0"] = lxc_net0(None, spec)
    for key in ("ssh-public-keys", "password"):
        if p.get(key):
            data[key] = p[key]
    if spec.pool:
        data["pool"] = spec.pool
    return data


def _hardware(spec: GuestSpec) -> dict[str, Any]:
    """Settings a creation and a clone's config take alike."""
    p, data = spec.params, dict[str, Any]()
    if spec.cores:
        data["cores"] = spec.cores
    if p.get("memory"):
        data["memory"] = p["memory"]
    if p.get("swap") is not None:
        data["swap"] = p["swap"]  # 0 is a value: no swap at all
    if spec.dns:
        data["nameserver"] = spec.dns
    if spec.search_domain:
        data["searchdomain"] = spec.search_domain
    if spec.features:
        data["features"] = spec.features
    if spec.tags:
        data["tags"] = ";".join(spec.tags)
    return data


async def _wait_ct_ip(client: ProxmoxClient, node: str, vmid: int, timeout: int) -> str | None:
    """First IPv4 of the container besides loopback, without its prefix."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for iface in await client.get_container_interfaces(node, vmid):
                if iface.get("name") == "lo":
                    continue
                for addr in iface.get("ip-addresses") or []:
                    ip = str(addr.get("ip-address", "")).split("/")[0]
                    if addr.get("ip-address-type") in ("inet", "ipv4") and not ip.startswith(("127.", "169.254.")):
                        return ip
        except PVECliError:
            pass
        await asyncio.sleep(3)
    return None


async def create_ct(facts: Facts, spec: GuestSpec, say: Say, out: Outcome) -> None:
    """Create or clone, configure, start. Raises on the first failure; `out`
    tells what exists by then."""
    client = facts.client
    node, target = spec.src["node"], spec.src["target"]
    newid = spec.vmid or await facts.next_id()
    spec.vmid = out.vmid = newid
    out.node = target

    if spec.src["mode"] == "create":
        upid = await client.post(f"/nodes/{node}/lxc", data=_create_data(spec, newid))
        out.created = True
        say("step", f"Creating CT {newid} from {spec.template}...")
        await client.wait_for_task(node, upid, timeout=1800)
    else:
        tid = int(spec.template)
        data: dict[str, Any] = {"newid": newid}
        if spec.name:
            data["hostname"] = spec.name
        # Sent even when 0: the client drops a false 'full', and the clone
        # mode must not depend on PVE's guess
        data["full"] = spec.src["full"]
        if spec.src["full"] and spec.storage:
            data["storage"] = spec.storage
        if target != node:
            data["target"] = target
        if spec.pool:
            data["pool"] = spec.pool
        # Two clones of one CT template at once: the second fails with
        # "CT is locked (disk)" (seen on PVE 9.1), so they take turns
        async with facts.clone_lock(tid):
            upid = await client.post(f"/nodes/{node}/lxc/{tid}/clone", data=data)
            out.created = True
            say("step", f"Cloning CT template {tid} to CT {newid} ({'full' if data['full'] else 'linked'})...")
            await client.wait_for_task(node, upid, timeout=1800)
        cfg = await client.get(f"/nodes/{target}/lxc/{newid}/config") or {}
        settings = _hardware(spec)
        if cfg.get("net0") or _net_wanted(spec):
            net0 = lxc_net0(cfg.get("net0"), spec)
            if net0 and net0 != cfg.get("net0"):
                settings["net0"] = net0
        if settings:
            await client.put(f"/nodes/{target}/lxc/{newid}/config", data=settings)
        current = extract_size(str(cfg.get("rootfs", "")))
        if spec.disk_size and (not current or size_bytes(spec.disk_size) > size_bytes(current)):
            # A task on PVE: the start must wait for it
            upid = await client.put(f"/nodes/{target}/lxc/{newid}/resize", data={"disk": "rootfs", "size": spec.disk_size})
            if upid:
                await client.wait_for_task(target, upid, timeout=600)
    say("ok", f"CT {newid} ({spec.name or 'CT' + str(newid)}) created on {target}")

    if spec.start:
        upid = await client.start_container(target, newid)
        await client.wait_for_task(target, upid, timeout=300)
        # The task can end OK on a container that stops right away
        status = (await client.get_container_status(target, newid) or {}).get("status")
        if status != "running":
            raise PVECliError(f"CT {newid} did not start: its status is {status}, not running")
        say("ok", f"CT {newid} started")
        if spec.wait_ip:
            say("step", f"Waiting up to {spec.wait_ip}s for its IPv4 address...")
            out.ip = await _wait_ct_ip(client, target, newid, spec.wait_ip)
            if out.ip:
                say("ok", f"CT {newid} is up at {out.ip}")
            else:
                say("warn", f"No IPv4 address on CT {newid} yet (DHCP pending, or no network). "
                            f"Look for it with: pvecli find {newid}")


def ct_command(spec: GuestSpec, profile: str | None) -> str:
    """The ct deploy command that makes the same container again. The
    password is never in it: only the variable holding it."""
    q = shlex.quote
    parts = [f"pvecli ct deploy {q(spec.template)}"]

    def opt(flag: str, value: Any) -> None:
        if value is not None and value != "":
            parts.append(f"{flag} {q(str(value))}")

    opt("--hostname", spec.name)
    opt("--ctid", spec.vmid)
    opt("--node", spec.node or spec.src.get("target"))
    opt("--storage", spec.storage)
    if spec.full:
        parts.append("--full")
    for flag, value in (("--disk-size", spec.disk_size), ("--cores", spec.cores), ("--memory", spec.memory),
                        ("--swap", spec.swap), ("--ip", spec.ip), ("--gw", spec.gw), ("--ip6", spec.ip6),
                        ("--gw6", spec.gw6), ("--bridge", spec.bridge), ("--vlan", spec.vlan), ("--dns", spec.dns),
                        ("--search-domain", spec.search_domain)):
        opt(flag, value)
    for key in spec.ssh_keys:
        opt("--ssh-key", key)
    opt("--password-env", spec.password_env)
    if spec.unprivileged is not None:
        parts.append("--unprivileged" if spec.unprivileged else "--privileged")
    opt("--features", spec.features)
    opt("--tags", ",".join(spec.tags))
    opt("--pool", spec.pool)
    if not spec.start:
        parts.append("--no-start")
    if spec.wait_ip:
        opt("--wait-ip", spec.wait_ip)
    if profile:
        # Without it a copy-paste would target the default profile
        parts.append(f"-p {q(profile)}")
    return " \\\n    ".join(parts)


@app.command("deploy")
@async_to_sync
async def ct_deploy(
    template: str = typer.Argument(
        ..., help="File template (local:vztmpl/debian-13-standard_...tar.zst) or CT template ID"),
    hostname: str = typer.Option(None, "--hostname", "-hn", help="Hostname (default: PVE's CT<id>, or the template's)"),
    ctid: int = typer.Option(None, "--ctid", "-ci", help="ID of the new container (default: next free)"),
    node: str = typer.Option(
        None, "--node", "-n", help="Node (default: the CT template's, or the only online node for a file template)"),
    storage: str = typer.Option(
        None, "--storage", "-s",
        help="Root disk storage (default: PVE's 'local' for a file template; on a clone, means a full clone there)"),
    full: bool = typer.Option(False, "--full", "-fu", help="Full clone of a CT template (default: linked when possible)"),
    disk_size: str = typer.Option(None, "--disk-size", "-dz", help="Root disk size, whole GiB (PVE default: 4G); a clone only grows"),
    cores: int = typer.Option(None, "--cores", "-co", help="CPU cores (PVE default: all of the host)"),
    memory: str = typer.Option(None, "--memory", "-me", help="Memory: 1G, 512M... (PVE default: 512M)"),
    swap: str = typer.Option(None, "--swap", "-sw", help="Swap: 512M, 0... (PVE default: 512M)"),
    ip: str = typer.Option(None, "--ip", "-ip", help="IPv4 CIDR (10.0.0.21/24), dhcp or manual"),
    gw: str = typer.Option(None, "--gw", help="IPv4 gateway"),
    ip6: str = typer.Option(None, "--ip6", "-ip6", help="IPv6 CIDR, dhcp, auto or manual"),
    gw6: str = typer.Option(None, "--gw6", help="IPv6 gateway"),
    bridge: str = typer.Option(None, "--bridge", "-br", help=f"Bridge or SDN vnet (default {DEFAULT_BRIDGE} for a new NIC)"),
    vlan: int = typer.Option(None, "--vlan", "-vl", help="VLAN tag of net0"),
    dns: str = typer.Option(None, "--dns", help="DNS server(s), space separated (PVE default: the host's)"),
    search_domain: str = typer.Option(None, "--search-domain", "-sd", help="DNS search domain"),
    ssh_key: list[str] = typer.Option(
        None, "--ssh-key", "-sk", help="Public key file or key for root (repeatable; file template only)"),
    password_env: str = typer.Option(
        None, "--password-env", "-pe", help="Env var holding the root password (file template only)"),
    unprivileged: bool = typer.Option(
        None, "--unprivileged/--privileged", "-up/-pr", help="Unprivileged container (PVE default at creation: unprivileged)"),
    features: str = typer.Option(None, "--features", help="Features, e.g. nesting=1 or nesting=1,keyctl=1"),
    tags: str = typer.Option(None, "--tags", "-tgs", help="Tags, comma separated"),
    pool: str = typer.Option(None, "--pool", "-pl", help="Resource pool"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start the container once configured"),
    wait_ip: int = typer.Option(0, "--wait-ip", "-wi", help="Seconds to wait for its IPv4 address (0: don't)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Create a ready-to-use container in one command.

    From a file template (POST /lxc: keys and root password possible) or a
    CT template (clone, then config). Every value is checked before anything
    is created, then the command to do it again is printed.

    Examples:
        pvecli ct deploy local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst --hostname web01 \\
            --node pve1 --storage local-lvm --ip dhcp --ssh-key ~/.ssh/id_ed25519.pub
        pvecli ct deploy 920 --hostname web02 --ip 10.0.0.22/24 --gw 10.0.0.1 --disk-size 8G --wait-ip 60
    """
    spec = GuestSpec(
        kind="ct", name=hostname, template=template, profile=profile, vmid=ctid, node=node, storage=storage,
        full=full, cores=cores, memory=memory, swap=swap, disk_size=disk_size, tags=split_tags(tags), pool=pool,
        ip=ip, gw=gw, ip6=ip6, gw6=gw6, dns=dns, search_domain=search_domain, ssh_keys=list(ssh_key or []),
        password_env=password_env, bridge=bridge, vlan=vlan, unprivileged=unprivileged, features=features,
        start=start, wait_ip=wait_ip,
    )
    errors = prepare_ct(spec)
    if errors:
        fail(errors)
    config = ConfigManager()
    try:
        async with ProxmoxClient(config.get_profile(profile)) as client:
            facts = Facts(client, profile)
            errors = await validate_ct(facts, spec)
            if errors:
                fail(errors)
            await create_ct(facts, spec, console_say, Outcome())
    except (PVECliError, ValueError) as e:
        fail([str(e)])
    console.print(f"\n[dim]{escape(ct_command(spec, profile or config.get().default_profile))}[/dim]\n", highlight=False)
