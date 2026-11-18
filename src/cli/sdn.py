"""SDN management commands.

Read commands work anywhere. Every write command refuses to run unless the
profile carries `sdn_write: true`, because applying an SDN configuration
reloads the network on every node of the cluster at once.
"""

import asyncio
import re

import typer
from rich.table import Table

from ..api.client import ProxmoxClient, is_not_implemented
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    confirm,
    console,
    emit_json,
    menu_prompt,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import select_menu

app = typer.Typer(
    help="Manage SDN (zones, vnets, subnets)",
    no_args_is_help=True,
    cls=ordered_group(["list", "zones", "info", "health", "zone", "vnet", "subnet", "apply", "discard", "unlock"]),
)
zone_app = typer.Typer(help="Manage SDN zones", no_args_is_help=True)
vnet_app = typer.Typer(help="Manage SDN vnets", no_args_is_help=True)
subnet_app = typer.Typer(help="Manage SDN subnets", no_args_is_help=True)
app.add_typer(zone_app, name="zone")
app.add_typer(vnet_app, name="vnet")
app.add_typer(subnet_app, name="subnet")

# Zone types and the extra parameters each one needs. Taken from
# POST /cluster/sdn/zones in the published API schema.
_ZONE_TYPES = {
    "simple": {
        "label": "Simple (isolated bridge, routed or NAT)",
        "asks": (),
    },
    "vlan": {
        "label": "VLAN (tag on an existing bridge)",
        "asks": ("bridge",),
    },
    "qinq": {
        "label": "QinQ (stacked VLANs)",
        "asks": ("bridge", "tag"),
    },
    "vxlan": {
        "label": "VXLAN (layer 2 overlay between nodes)",
        "asks": ("peers",),
    },
    "evpn": {
        "label": "EVPN (routed overlay, needs a controller)",
        "asks": ("controller", "vrf-vxlan"),
    },
}


_SDN_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*[a-zA-Z0-9]$")


def _valid_sdn_id(name: str) -> bool:
    """Zone and vnet identifiers: 2 to 8 chars, alphanumeric, no leading digit."""
    return bool(name) and len(name) <= 8 and bool(_SDN_ID.match(name))


# A VLAN tag is 12 bits, a VXLAN VNI is 24. The API schema accepts the widest
# of the two for every zone type, so an out-of-range VLAN tag is only rejected
# at apply time, on every node at once.
_TAG_RANGE = {
    "vlan": (1, 4094),
    "qinq": (1, 4094),
    "vxlan": (1, 16777215),
    "evpn": (1, 16777215),
}


def _ask_int(
    label: str,
    default: str | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    """Ask for an integer, reprompting instead of crashing on bad input."""
    while True:
        raw = menu_prompt(label, default=default)
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            print_error("Enter a whole number")
            continue
        if minimum is not None and maximum is not None and not minimum <= value <= maximum:
            print_error(f"Enter a value between {minimum} and {maximum}")
            continue
        return value


def _uses_vnet(net_value: str, vnet: str) -> bool:
    """True when a guest network line is bridged on exactly this vnet.

    Substring matching would make vnet 'cli1' match 'bridge=cli12', and vnet
    names are capped at 8 characters so shared prefixes are common.
    """
    for part in str(net_value).split(","):
        key, _, value = part.partition("=")
        if key.strip() == "bridge" and value.strip() == vnet:
            return True
    return False


def _format_dhcp_ranges(raw) -> str:
    """Render DHCP ranges as 'start - end'.

    The API returns them either as a string or as dicts carrying
    start-address and end-address.
    """
    if not raw:
        return "-"
    entries = raw if isinstance(raw, list) else [raw]
    out = []
    for entry in entries:
        if isinstance(entry, dict):
            start = entry.get("start-address", "?")
            end = entry.get("end-address", "?")
            out.append(f"{start} - {end}")
        else:
            out.append(str(entry))
    return ", ".join(out)


def _require_write(profile_config, profile_name: str | None) -> None:
    """Refuse any SDN write unless the profile opted in.

    Applying SDN reloads the network on every node, so this is deliberately
    opt-in per profile rather than a global flag or a --force.
    """
    if not getattr(profile_config, "sdn_write", False):
        print_error(
            f"SDN writes are disabled for profile '{profile_name or 'default'}'."
        )
        print_info(
            "Add 'sdn_write: true' to this profile in ~/.config/pvecli/config.yaml "
            "to allow it. Leave it off for production clusters."
        )
        raise typer.Exit(1)


async def _pending_changes(client: ProxmoxClient) -> tuple[list, list, list]:
    """Zones, vnets and subnets carrying an unapplied change.

    A pending entry is flagged by a 'state' field (new, changed, deleted).
    Subnets have to be asked for explicitly: a subnet added alone does not
    show up in the pending view of its vnet.
    """
    zones = [z for z in (await client.get_sdn_zones(pending=True) or []) if z.get("state")]
    vnets_all = await client.get_sdn_vnets(pending=True) or []
    vnets = [v for v in vnets_all if v.get("state")]
    subnets: list = []
    for v in vnets_all:
        name = v.get("vnet")
        if not name:
            continue
        try:
            for sub in await client.get_sdn_subnets(name, pending=True) or []:
                if sub.get("state"):
                    subnets.append({**sub, "vnet": name})
        except PVECliError:
            continue
    return zones, vnets, subnets


async def _vnet_status_by_node(client: ProxmoxClient, nodes: list[str]) -> dict[str, dict]:
    """Per vnet, its status on each node: {vnet: {node: status}}."""
    result: dict[str, dict] = {}
    for node in nodes:
        try:
            zones = await client.get_sdn_zone_status(node) or []
        except PVECliError:
            continue
        for z in zones:
            zone = z.get("zone")
            if not zone:
                continue
            try:
                content = await client.get_sdn_zone_content(node, zone) or []
            except PVECliError:
                continue
            for entry in content:
                vnet = entry.get("vnet")
                if vnet:
                    result.setdefault(vnet, {})[node] = entry.get("status", "?")
    return result


@app.command("list")
@fleet(parallel=True)
@async_to_sync
async def sdn_list(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """List vnets with their zone and their status on every node."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            vnets = await client.get_sdn_vnets() or []
            if not vnets:
                print_info("No SDN vnet defined")
                return

            nodes = sorted(
                n.get("node", "")
                for n in await client.get_nodes() or []
                if n.get("status") == "online"
            )
            statuses = await _vnet_status_by_node(client, nodes)

            if json_output:
                emit_json([
                    {
                        "vnet": v.get("vnet"),
                        "zone": v.get("zone"),
                        "tag": v.get("tag"),
                        "alias": v.get("alias"),
                        "vlanaware": bool(v.get("vlanaware")),
                        "status": statuses.get(v.get("vnet", ""), {}),
                    }
                    for v in vnets
                ])
                return

            table = Table(title="SDN vnets", show_header=True, header_style="bold cyan")
            table.add_column("Vnet", style="cyan")
            table.add_column("Zone")
            table.add_column("Tag/VNI", justify="right")
            table.add_column("Alias")
            table.add_column("VLAN aware")
            table.add_column("Nodes")

            for v in sorted(vnets, key=lambda x: x.get("vnet", "")):
                name = v.get("vnet", "")
                per_node = statuses.get(name, {})
                ok = [n for n, s in per_node.items() if s == "available"]
                if not per_node:
                    node_col = "[yellow]not deployed[/yellow]"
                elif len(ok) == len(nodes):
                    node_col = f"[green]{len(ok)}/{len(nodes)}[/green]"
                else:
                    ko = sorted(set(nodes) - set(ok))
                    node_col = f"[red]{len(ok)}/{len(nodes)}[/red] missing: {', '.join(ko)}"

                table.add_row(
                    name,
                    v.get("zone", "-"),
                    str(v.get("tag", "-")),
                    v.get("alias", "") or "-",
                    "yes" if v.get("vlanaware") else "no",
                    node_col,
                )
            console.print(table)

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@app.command("zones")
@fleet(parallel=True)
@async_to_sync
async def sdn_zones(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """List SDN zones and where they are actually deployed."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            zones = await client.get_sdn_zones() or []
            if not zones:
                print_info("No SDN zone defined")
                return

            nodes = sorted(
                n.get("node", "")
                for n in await client.get_nodes() or []
                if n.get("status") == "online"
            )
            per_node: dict[str, dict[str, str]] = {}
            for node in nodes:
                try:
                    for z in await client.get_sdn_zone_status(node) or []:
                        per_node.setdefault(z.get("zone", ""), {})[node] = z.get("status", "?")
                except PVECliError:
                    continue

            if json_output:
                emit_json([
                    {**z, "status": per_node.get(z.get("zone", ""), {})} for z in zones
                ])
                return

            table = Table(title="SDN zones", show_header=True, header_style="bold cyan")
            table.add_column("Zone", style="cyan")
            table.add_column("Type")
            table.add_column("Restricted to")
            table.add_column("MTU", justify="right")
            table.add_column("Deployed on")

            for z in sorted(zones, key=lambda x: x.get("zone", "")):
                st = per_node.get(z.get("zone", ""), {})
                ok = [n for n, s in st.items() if s == "available"]
                deployed = (
                    "[yellow]nowhere[/yellow]"
                    if not st
                    else f"[green]{len(ok)}/{len(nodes)}[/green]"
                    if len(ok) == len(nodes)
                    else f"[red]{len(ok)}/{len(nodes)}[/red]"
                )
                table.add_row(
                    z.get("zone", ""),
                    z.get("type", "-"),
                    z.get("nodes", "") or "all nodes",
                    str(z.get("mtu", "-")),
                    deployed,
                )
            console.print(table)

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@app.command("info")
@async_to_sync
async def sdn_info(
    vnet: str = typer.Argument(None, help="Vnet name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Detail a vnet: definition, subnets and the guests attached to it."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            vnets = await client.get_sdn_vnets() or []
            names = sorted(v.get("vnet", "") for v in vnets if v.get("vnet"))
            if not names:
                print_info("No SDN vnet defined")
                return

            if vnet is None:
                idx = select_menu(names, "  Vnet:")
                if idx is None:
                    print_cancelled()
                    return
                vnet = names[idx]
            elif vnet not in names:
                print_error(f"Vnet '{vnet}' not found. Available: {', '.join(names)}")
                raise typer.Exit(1)

            definition = await client.get_sdn_vnet(vnet) or {}
            subnets = await client.get_sdn_subnets(vnet) or []

            # Which guests actually sit on this vnet. The API has no reverse
            # index, so the guest configs are the only source of truth.
            attached: list[dict] = []
            for res in await client.get_cluster_resources(resource_type="vm") or []:
                node, vmid = res.get("node"), res.get("vmid")
                if not node or vmid is None:
                    continue
                try:
                    cfg = (
                        await client.get_vm_config(node, vmid)
                        if res.get("type") == "qemu"
                        else await client.get_container_config(node, vmid=vmid)
                    )
                except PVECliError:
                    continue
                for key, value in (cfg or {}).items():
                    if key.startswith("net") and _uses_vnet(value, vnet):
                        tag = ""
                        for part in str(value).split(","):
                            if part.startswith("tag="):
                                tag = part[4:]
                        attached.append({
                            "vmid": vmid,
                            "name": res.get("name", ""),
                            "type": "VM" if res.get("type") == "qemu" else "CT",
                            "node": node,
                            "iface": key,
                            "tag": tag,
                            "status": res.get("status", ""),
                        })

            if json_output:
                emit_json({
                    "vnet": vnet,
                    "definition": definition,
                    "subnets": subnets,
                    "guests": attached,
                })
                return

            console.print(f"\n[bold cyan]═══ Vnet {vnet} ═══[/bold cyan]\n")
            console.print(f"  [bold]Zone:[/bold]       {definition.get('zone', '-')}")
            console.print(f"  [bold]Tag/VNI:[/bold]    {definition.get('tag', '-')}")
            console.print(f"  [bold]Alias:[/bold]      {definition.get('alias', '') or '-'}")
            console.print(
                f"  [bold]VLAN aware:[/bold] {'yes' if definition.get('vlanaware') else 'no'}"
            )

            if subnets:
                st = Table(title="Subnets", show_header=True, header_style="bold cyan")
                st.add_column("Subnet", style="cyan")
                st.add_column("Gateway")
                st.add_column("SNAT")
                st.add_column("DHCP range")
                for sub in subnets:
                    st.add_row(
                        sub.get("cidr") or sub.get("subnet", "-"),
                        sub.get("gateway", "-") or "-",
                        "yes" if sub.get("snat") else "no",
                        _format_dhcp_ranges(sub.get("dhcp-range")),
                    )
                console.print(st)
            else:
                print_info("No subnet on this vnet")

            if attached:
                gt = Table(
                    title=f"Guests on {vnet} ({len(attached)})",
                    show_header=True,
                    header_style="bold cyan",
                )
                gt.add_column("ID", style="cyan", justify="right")
                gt.add_column("Name")
                gt.add_column("Type")
                gt.add_column("Node")
                gt.add_column("Interface")
                gt.add_column("VLAN")
                gt.add_column("Status")
                for g in sorted(attached, key=lambda x: x["vmid"]):
                    gt.add_row(
                        str(g["vmid"]), g["name"], g["type"], g["node"],
                        g["iface"], g["tag"] or "-", g["status"],
                    )
                console.print(gt)
            else:
                print_info("No guest attached to this vnet")

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@app.command("health")
@fleet(parallel=True)
@async_to_sync
async def sdn_health(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Report unapplied changes and vnets missing on some nodes."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            nodes = sorted(
                n.get("node", "")
                for n in await client.get_nodes() or []
                if n.get("status") == "online"
            )

            pending_zones, pending_vnets, pending_subnets = await _pending_changes(client)

            statuses = await _vnet_status_by_node(client, nodes)
            # Start from the declared vnets: one deployed nowhere has no entry
            # in statuses at all and would otherwise look healthy.
            declared = [v.get("vnet", "") for v in await client.get_sdn_vnets() or []]
            incomplete = {
                vnet: sorted(
                    set(nodes)
                    - {n for n, s in (statuses.get(vnet) or {}).items() if s == "available"}
                )
                for vnet in declared
            }
            incomplete = {k: v for k, v in incomplete.items() if v}

            errors = {
                vnet: {n: s for n, s in per.items() if s not in ("available", None)}
                for vnet, per in statuses.items()
            }
            errors = {k: v for k, v in errors.items() if v}

            # dnsmasq ignores every MAC the IPAM does not know: a DHCP zone
            # without IPAM looks fine everywhere and never leases an address.
            dhcp_no_ipam = sorted(
                z.get("zone", "")
                for z in await client.get_sdn_zones() or []
                if z.get("dhcp") and not z.get("ipam")
            )

            if json_output:
                emit_json({
                    "dhcp_without_ipam": dhcp_no_ipam,
                    "pending_zones": pending_zones,
                    "pending_vnets": pending_vnets,
                    "pending_subnets": pending_subnets,
                    "incomplete": incomplete,
                    "errors": errors,
                })
                return

            table = Table(title="SDN health", show_header=True, header_style="bold cyan")
            table.add_column("Check", style="cyan")
            table.add_column("Result")
            table.add_column("Detail")

            pending_total = len(pending_zones) + len(pending_vnets) + len(pending_subnets)
            table.add_row(
                "Pending changes",
                "[yellow]yes[/yellow]" if pending_total else "[green]none[/green]",
                (
                    ", ".join(
                        [f"zone {z.get('zone')} ({z.get('state')})" for z in pending_zones]
                        + [f"vnet {v.get('vnet')} ({v.get('state')})" for v in pending_vnets]
                        + [
                            f"subnet {s.get('cidr') or s.get('subnet')} on {s.get('vnet')}"
                            f" ({s.get('state')})"
                            for s in pending_subnets
                        ]
                    )
                    if pending_total
                    else "configuration is applied"
                ),
            )
            table.add_row(
                "Vnet coverage",
                "[red]partial[/red]" if incomplete else "[green]complete[/green]",
                (
                    "; ".join(f"{v} missing on {', '.join(n)}" for v, n in incomplete.items())
                    if incomplete
                    else f"every vnet is available on the {len(nodes)} node(s)"
                ),
            )
            table.add_row(
                "Vnet errors",
                "[red]yes[/red]" if errors else "[green]none[/green]",
                "; ".join(f"{v}: {n}" for v, n in errors.items()) if errors else "-",
            )
            table.add_row(
                "DHCP zones",
                "[red]no IPAM[/red]" if dhcp_no_ipam else "[green]ok[/green]",
                (
                    f"{', '.join(dhcp_no_ipam)}: DHCP enabled without IPAM, no lease will "
                    "ever be given. PVE only adds an IPAM to a zone without subnets: remove "
                    "them, run 'pvecli sdn zone set <zone> --dhcp', add them back, apply"
                    if dhcp_no_ipam
                    else "-"
                ),
            )
            console.print(table)

            if pending_total:
                print_warning(
                    "Pending changes are not active yet. Apply them with: pvecli sdn apply"
                )

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


# ── writes ───────────────────────────────────────────────────────────────


@app.command("apply")
@async_to_sync
async def sdn_apply(
    force: bool = typer.Option(False, "--force", "-f", is_flag=True, help="Re-apply even when nothing is pending, to regenerate the node configuration"),
    diff: bool = typer.Option(True, "--diff/--no-diff", help="Show what the reload changes on a node first (PVE 9.2+)"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip the typed confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Apply the pending SDN configuration on every node of the cluster.

    On PVE 9.2+ the network and FRR changes are previewed first, and the
    global SDN lock is taken for the duration of the apply, so a concurrent
    edit from the web UI or another tool cannot slip in between.
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            pending_zones, pending_vnets, pending_subnets = await _pending_changes(client)
            if not pending_zones and not pending_vnets and not pending_subnets and not force:
                print_info("Nothing pending, the SDN configuration is already applied")
                print_info("Use --force to regenerate the configuration on the nodes anyway")
                return

            try:
                totem = await client.get_cluster_totem() or {}
            except PVECliError:
                totem = {}
            cluster_name = totem.get("cluster_name") or profile_config.host

            nodes = [
                n.get("node", "")
                for n in await client.get_nodes() or []
                if n.get("status") == "online"
            ]
            if not nodes:
                print_error("No online node to run the SDN reload on")
                raise typer.Exit(1)

            console.print("\n[bold cyan]═══ Apply SDN ═══[/bold cyan]\n")
            for z in pending_zones:
                console.print(f"  zone {z.get('zone')} [yellow]{z.get('state')}[/yellow]")
            for v in pending_vnets:
                console.print(f"  vnet {v.get('vnet')} [yellow]{v.get('state')}[/yellow]")
            for sub in pending_subnets:
                console.print(
                    f"  subnet {sub.get('cidr') or sub.get('subnet')} on {sub.get('vnet')}"
                    f" [yellow]{sub.get('state')}[/yellow]"
                )
            if not pending_zones and not pending_vnets and not pending_subnets:
                console.print("  [dim]nothing pending, forced regeneration[/dim]")
            console.print(f"\n  [bold]Cluster:[/bold] {cluster_name}")
            console.print(f"  [bold]Nodes:[/bold]   {', '.join(nodes)}")
            if diff:
                await _show_dry_run(client, nodes[0])
            print_warning(
                "Applying reloads the network configuration on every node listed above. "
                "Guests attached to a modified vnet can lose connectivity for a moment."
            )

            if not yes:
                typed = menu_prompt(f"  Type the cluster name '{cluster_name}' to confirm")
                if typed != cluster_name:
                    print_error("Name does not match")
                    print_cancelled()
                    raise typer.Exit(1)

            # PUT /cluster/sdn returns a UPID: the reload runs as a task and
            # announcing success before it finishes would hide a failed
            # ifreload on any node.
            token = await _take_sdn_lock(client)
            try:
                upid = await client.apply_sdn(lock_token=token)
            except PVECliError:
                if token:
                    # The apply never committed: give the lock back
                    await client.unlock_sdn(lock_token=token)
                raise
            try:
                result = await client.wait_for_task(nodes[0], upid, timeout=300)
            except PVECliError as e:
                print_error(f"SDN reload failed: {e}")
                print_info(
                    "The configuration is still pending. Check the task log with: "
                    "pvecli cluster tasks"
                )
                raise typer.Exit(1)

            if (result or {}).get("exitstatus") not in ("OK", None):
                print_error(f"SDN reload ended with: {(result or {}).get('exitstatus')}")
                raise typer.Exit(1)
            print_success("SDN configuration applied")

            # Poll instead of sleeping once: a large cluster takes longer than
            # any fixed delay we could pick.
            declared = [v.get("vnet", "") for v in await client.get_sdn_vnets() or []]
            bad: dict[str, list[str]] = {}
            for _ in range(12):
                statuses = await _vnet_status_by_node(client, nodes)
                bad = {
                    vnet: sorted(
                        set(nodes)
                        - {n for n, s in (statuses.get(vnet) or {}).items() if s == "available"}
                    )
                    for vnet in declared
                }
                bad = {v: n for v, n in bad.items() if n}
                if not bad:
                    break
                await asyncio.sleep(5)

            if bad:
                print_warning(
                    "Some vnets are still not available: "
                    + "; ".join(f"{v} on {', '.join(n)}" for v, n in bad.items())
                )
            else:
                print_success(f"Every vnet is available on the {len(nodes)} node(s)")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


def _explain_sdn_error(err: Exception) -> str:
    """Turn the raw lock error of PVE into something actionable."""
    text = str(err)
    if "lock token" in text:
        return (
            "The SDN configuration is locked by another session (web UI, PDM, "
            "Terraform...). Wait for it to finish, or, if it is stale, release it "
            "with: pvecli sdn unlock"
        )
    return text


async def _take_sdn_lock(client: ProxmoxClient) -> str | None:
    """Take the global SDN lock, or None on versions without one.

    Refuses clearly when another session (web UI, PDM, Terraform) holds it,
    instead of the bare API error the apply would end with.
    """
    try:
        return await client.lock_sdn(allow_pending=True)
    except PVECliError as e:
        if is_not_implemented(e):
            return None
        if "lock" in str(e).lower():
            raise PVECliError(
                "The SDN configuration is locked by another session (web UI, PDM, "
                f"Terraform...): {e}. Wait for it to finish, or, if it is stale, "
                "release it with: pvecli sdn unlock"
            )
        raise


async def _show_dry_run(client: ProxmoxClient, node: str, max_lines: int = 60) -> None:
    """Print what the reload would change on one node (PVE 9.2+, skipped before)."""
    try:
        result = await client.get_sdn_dry_run(node)
    except PVECliError as e:
        if is_not_implemented(e):
            return
        print_warning(f"Could not preview the changes: {e}")
        return
    shown = False
    for key, title in (("interfaces-diff", "Network interfaces"), ("frr-diff", "FRR routing")):
        text = (result.get(key) or "").rstrip()
        if not text:
            continue
        shown = True
        lines = text.splitlines()
        console.print(f"\n  [bold]{title} on {node}:[/bold]")
        for line in lines[:max_lines]:
            style = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
            console.print(f"    {line}", style=style, markup=False, highlight=False)
        if len(lines) > max_lines:
            console.print(f"    [dim]... {len(lines) - max_lines} more line(s)[/dim]")
    if not shown:
        console.print(f"\n  [dim]No change to the network or FRR configuration of {node}[/dim]")


@app.command("unlock")
@async_to_sync
async def sdn_unlock(
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Release a stale global SDN lock left by another session."""
    config_manager = ConfigManager()
    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)
        async with ProxmoxClient(profile_config) as client:
            if not yes and not confirm(
                "Release the SDN lock? Only do this when the session holding it is gone.",
                default=False,
            ):
                print_cancelled()
                return
            try:
                await client.unlock_sdn(force=True)
            except PVECliError as e:
                if is_not_implemented(e):
                    print_info("This PVE version has no global SDN lock: nothing to release")
                    return
                raise
            print_success("SDN lock released")
    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@app.command("discard")
@async_to_sync
async def sdn_discard(
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Drop every pending SDN change, back to the applied configuration."""
    config_manager = ConfigManager()
    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)
        async with ProxmoxClient(profile_config) as client:
            pending_zones, pending_vnets, pending_subnets = await _pending_changes(client)
            pending = (
                [f"zone {z.get('zone')} ({z.get('state')})" for z in pending_zones]
                + [f"vnet {v.get('vnet')} ({v.get('state')})" for v in pending_vnets]
                + [
                    f"subnet {x.get('cidr') or x.get('subnet')} on {x.get('vnet')} ({x.get('state')})"
                    for x in pending_subnets
                ]
            )
            if not pending:
                print_info("Nothing pending, the SDN configuration is already applied")
                return
            console.print("\n[bold]Pending changes to drop:[/bold]")
            for line in pending:
                console.print(f"  {line}")
            if not yes and not confirm("\nDrop all of them?", default=False):
                print_cancelled()
                return
            try:
                await client.rollback_sdn()
            except PVECliError as e:
                if is_not_implemented(e):
                    print_error(
                        "This PVE version cannot drop pending SDN changes through the API: "
                        "remove each pending object instead"
                    )
                    raise typer.Exit(1)
                raise
            print_success(f"{len(pending)} pending change(s) dropped")
    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@zone_app.command("add")
@async_to_sync
async def sdn_zone_add(
    zone: str = typer.Argument(None, help="Zone name (8 characters max)"),
    zone_type: str = typer.Option(None, "--type", "-t", help=f"Type: {', '.join(_ZONE_TYPES)}"),
    bridge: str = typer.Option(None, "--bridge", "-b", help="Bridge, for vlan and qinq zones"),
    peers: str = typer.Option(None, "--peers", help="Peer IPs, for vxlan zones, comma separated"),
    tag: int = typer.Option(None, "--tag", help="Service VLAN tag, for qinq zones"),
    controller: str = typer.Option(None, "--controller", help="Controller, for evpn zones"),
    vrf_vxlan: int = typer.Option(None, "--vrf-vxlan", help="VRF VNI, for evpn zones"),
    mtu: int = typer.Option(None, "--mtu", help="MTU of the vnet bridges"),
    nodes: str = typer.Option(None, "--nodes", "-n", help="Restrict the zone to these nodes"),
    ipam: str = typer.Option(None, "--ipam", help="IPAM to use (default pve)"),
    dhcp: bool = typer.Option(False, "--dhcp", is_flag=True, help="Serve DHCP on this zone through dnsmasq (technology preview)"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Create an SDN zone. The change stays pending until 'sdn apply'."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            interactive = zone_type is None

            if zone_type is None:
                console.print("\n[bold cyan]═══ Add SDN zone ═══[/bold cyan]\n")
                keys = list(_ZONE_TYPES)
                idx = select_menu(
                    [f"{k.ljust(7)} {_ZONE_TYPES[k]['label']}" for k in keys], "  Zone type:"
                )
                if idx is None:
                    print_cancelled()
                    return
                zone_type = keys[idx]
            elif zone_type not in _ZONE_TYPES:
                print_error(
                    f"Unknown zone type '{zone_type}'. Available: {', '.join(_ZONE_TYPES)}"
                )
                raise typer.Exit(1)

            existing = {z.get("zone") for z in await client.get_sdn_zones() or []}
            if zone is None:
                zone = menu_prompt("  Zone name (8 characters max)")
            if not zone:
                print_cancelled()
                return
            if not _valid_sdn_id(zone):
                print_error(
                    "A zone name must be 2 to 8 characters, letters and digits only, "
                    "and must start with a letter"
                )
                raise typer.Exit(1)
            if zone in existing:
                print_error(f"Zone '{zone}' already exists")
                raise typer.Exit(1)

            asks = _ZONE_TYPES[zone_type]["asks"]
            if "bridge" in asks and not bridge:
                if not interactive:
                    print_error(f"--bridge is required for a {zone_type} zone")
                    raise typer.Exit(1)
                online = sorted(
                    n.get("node", "")
                    for n in await client.get_nodes() or []
                    if n.get("status") == "online"
                )
                if not online:
                    print_error("No online node to read the bridge list from")
                    raise typer.Exit(1)
                node0 = online[0]
                from ._shared import bridge_choices

                names, items = bridge_choices(await client.get_network_interfaces(node0))
                idx = select_menu(items, "  Bridge:")
                if idx is None:
                    print_cancelled()
                    return
                bridge = names[idx]
            if "peers" in asks and not peers:
                if not interactive:
                    print_error("--peers is required for a vxlan zone")
                    raise typer.Exit(1)
                default_peers = ",".join(
                    sorted(
                        n.get("ip", "")
                        for n in await client.get_cluster_status() or []
                        if n.get("type") == "node" and n.get("ip")
                    )
                )
                peers = menu_prompt("  Peer IPs (comma separated)", default=default_peers)
            if "tag" in asks and tag is None:
                if not interactive:
                    print_error("--tag is required for a qinq zone")
                    raise typer.Exit(1)
                tag = _ask_int("  Service VLAN tag")
                if tag is None:
                    print_cancelled()
                    return
            if "controller" in asks and not controller:
                controllers = [
                    c.get("controller", "")
                    for c in (await client.get("/cluster/sdn/controllers") or [])
                ]
                if not controllers:
                    print_error(
                        "An evpn zone needs a controller, and none is defined. "
                        "Create one first (Datacenter > SDN > Controllers)."
                    )
                    raise typer.Exit(1)
                if not interactive:
                    print_error(
                        f"--controller is required for an evpn zone. Available: "
                        f"{', '.join(controllers)}"
                    )
                    raise typer.Exit(1)
                idx = select_menu(controllers, "  Controller:")
                if idx is None:
                    print_cancelled()
                    return
                controller = controllers[idx]
            if "vrf-vxlan" in asks and vrf_vxlan is None:
                if not interactive:
                    print_error("--vrf-vxlan is required for an evpn zone")
                    raise typer.Exit(1)
                vrf_vxlan = _ask_int("  VRF VXLAN id")
                if vrf_vxlan is None:
                    print_cancelled()
                    return

            params = {
                "bridge": bridge,
                "peers": peers,
                "tag": tag,
                "controller": controller,
                "vrf-vxlan": vrf_vxlan,
                "mtu": mtu,
                "nodes": nodes,
                # Same default as the web UI. Without an IPAM, dnsmasq only
                # serves MACs registered there, so a DHCP zone would ignore
                # every request (DHCPDISCOVER ... ignored).
                "ipam": ipam or "pve",
                # dnsmasq is the only backend the API accepts today
                "dhcp": "dnsmasq" if dhcp else None,
            }
            params = {k: v for k, v in params.items() if v is not None}

            console.print("\n[bold cyan]═══ Summary ═══[/bold cyan]\n")
            console.print(f"  [bold]Zone:[/bold] {zone}")
            console.print(f"  [bold]Type:[/bold] {zone_type}")
            for key, value in params.items():
                console.print(f"  [bold]{key}:[/bold] {value}")

            if not yes and not confirm("\nCreate this zone?", default=True):
                print_cancelled()
                return

            await client.create_sdn_zone(zone, zone_type, **params)
            print_success(f"Zone '{zone}' created (pending)")
            print_info("Activate it with: pvecli sdn apply")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@zone_app.command("set")
@async_to_sync
async def sdn_zone_set(
    zone: str = typer.Argument(None, help="Zone to modify"),
    dhcp: bool = typer.Option(None, "--dhcp/--no-dhcp", help="Enable or disable the dnsmasq DHCP backend"),
    mtu: int = typer.Option(None, "--mtu", help="MTU of the vnet bridges"),
    nodes: str = typer.Option(None, "--nodes", "-n", help="Restrict the zone to these nodes"),
    peers: str = typer.Option(None, "--peers", help="Peer IPs for a vxlan zone"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Change an existing SDN zone. The change stays pending until 'sdn apply'."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            zones = await client.get_sdn_zones() or []
            names = sorted(z.get("zone", "") for z in zones)
            if not names:
                print_info("No SDN zone defined")
                return
            if zone is None:
                idx = select_menu(names, "  Zone to modify:")
                if idx is None:
                    print_cancelled()
                    return
                zone = names[idx]
            elif zone not in names:
                print_error(f"Zone '{zone}' not found. Available: {', '.join(names)}")
                raise typer.Exit(1)

            params: dict = {"mtu": mtu, "nodes": nodes, "peers": peers}
            if dhcp is True:
                params["dhcp"] = "dnsmasq"
                current = next((z for z in zones if z.get("zone") == zone), {})
                if not current.get("ipam"):
                    # DHCP without IPAM hands out nothing: add the default one.
                    # PVE only accepts that on a zone that has no subnet yet.
                    zone_subnets = [
                        f"{sub.get('cidr') or sub.get('subnet')} on {v.get('vnet')}"
                        for v in await client.get_sdn_vnets() or []
                        if v.get("zone") == zone
                        for sub in await client.get_sdn_subnets(v.get("vnet", "")) or []
                    ]
                    if zone_subnets:
                        print_error(
                            f"Zone '{zone}' has no IPAM, so its DHCP would never lease an "
                            "address, and PVE refuses to add one while subnets exist "
                            f"({', '.join(zone_subnets)}). Remove the subnets, run this "
                            "command again, then add the subnets back and apply."
                        )
                        raise typer.Exit(1)
                    params["ipam"] = "pve"
            elif dhcp is False:
                # An empty value fails the enum check; 'delete' is how the API
                # drops a property.
                params["delete"] = "dhcp"
            params = {k: v for k, v in params.items() if v is not None}
            if not params:
                print_error("Nothing to change, pass at least one option")
                raise typer.Exit(1)

            console.print(f"\n  [bold]Zone:[/bold] {zone}")
            for key, value in params.items():
                label = "(cleared)" if key == "delete" else value
                console.print(f"  [bold]{'dhcp' if key == 'delete' else key}:[/bold] {label}")

            if not yes and not confirm("\nApply this change?", default=True):
                print_cancelled()
                return

            await client.update_sdn_zone(zone, **params)
            print_success(f"Zone '{zone}' updated (pending)")
            print_info("Activate it with: pvecli sdn apply")

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@zone_app.command("remove")
@async_to_sync
async def sdn_zone_remove(
    zone: str = typer.Argument(None, help="Zone to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Remove an SDN zone. Refused while vnets still use it."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            zones = sorted(z.get("zone", "") for z in await client.get_sdn_zones() or [])
            if not zones:
                print_info("No SDN zone defined")
                return
            if zone is None:
                idx = select_menu(zones, "  Zone to remove:")
                if idx is None:
                    print_cancelled()
                    return
                zone = zones[idx]
            elif zone not in zones:
                print_error(f"Zone '{zone}' not found")
                raise typer.Exit(1)

            users = [
                v.get("vnet")
                for v in await client.get_sdn_vnets() or []
                if v.get("zone") == zone
            ]
            if users:
                print_error(
                    f"Zone '{zone}' still holds vnet(s): {', '.join(str(u) for u in users)}. "
                    "Remove them first."
                )
                raise typer.Exit(1)

            if not yes and not confirm(f"Remove zone '{zone}'?", default=False):
                print_cancelled()
                return

            await client.delete_sdn_zone(zone)
            print_success(f"Zone '{zone}' removed (pending)")
            print_info("Apply the change with: pvecli sdn apply")

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@vnet_app.command("add")
@async_to_sync
async def sdn_vnet_add(
    vnet: str = typer.Argument(None, help="Vnet name (8 characters max)"),
    zone: str = typer.Option(None, "--zone", "-z", help="Zone this vnet belongs to"),
    tag: int = typer.Option(None, "--tag", help="VLAN tag or VXLAN VNI"),
    alias: str = typer.Option(None, "--alias", "-a", help="Free description"),
    vlanaware: bool = typer.Option(False, "--vlanaware", is_flag=True, help="Let guests tag their own VLANs"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Create a vnet in a zone. The change stays pending until 'sdn apply'."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            zones = await client.get_sdn_zones() or []
            zone_names = sorted(z.get("zone", "") for z in zones)
            if not zone_names:
                print_error("No SDN zone defined yet. Create one with: pvecli sdn zone add")
                raise typer.Exit(1)

            if zone is None:
                console.print("\n[bold cyan]═══ Add SDN vnet ═══[/bold cyan]\n")
                idx = select_menu(zone_names, "  Zone:")
                if idx is None:
                    print_cancelled()
                    return
                zone = zone_names[idx]
            elif zone not in zone_names:
                print_error(f"Zone '{zone}' not found. Available: {', '.join(zone_names)}")
                raise typer.Exit(1)

            existing = {v.get("vnet") for v in await client.get_sdn_vnets() or []}
            if vnet is None:
                vnet = menu_prompt("  Vnet name (8 characters max)")
            if not vnet:
                print_cancelled()
                return
            if not _valid_sdn_id(vnet):
                print_error(
                    "A vnet name must be 2 to 8 characters, letters and digits only, "
                    "and must start with a letter"
                )
                raise typer.Exit(1)
            if vnet in existing:
                print_error(f"Vnet '{vnet}' already exists")
                raise typer.Exit(1)

            zone_type = next((z.get("type") for z in zones if z.get("zone") == zone), "")
            tag_range = _TAG_RANGE.get(zone_type or "")
            if tag is None and tag_range:
                low, high = tag_range
                tag = _ask_int(
                    f"  {'VXLAN VNI' if zone_type in ('vxlan', 'evpn') else 'VLAN tag'}"
                    f" ({low}-{high})",
                    minimum=low,
                    maximum=high,
                )
            elif tag is not None and tag_range and not tag_range[0] <= tag <= tag_range[1]:
                print_error(
                    f"A {zone_type} zone takes a tag between {tag_range[0]} and "
                    f"{tag_range[1]}, got {tag}"
                )
                raise typer.Exit(1)

            params = {"tag": tag, "alias": alias}
            if vlanaware:
                params["vlanaware"] = 1
            params = {k: v for k, v in params.items() if v is not None}

            console.print("\n[bold cyan]═══ Summary ═══[/bold cyan]\n")
            console.print(f"  [bold]Vnet:[/bold] {vnet}")
            console.print(f"  [bold]Zone:[/bold] {zone} ({zone_type})")
            for key, value in params.items():
                console.print(f"  [bold]{key}:[/bold] {value}")

            if not yes and not confirm("\nCreate this vnet?", default=True):
                print_cancelled()
                return

            await client.create_sdn_vnet(vnet, zone, **params)
            print_success(f"Vnet '{vnet}' created (pending)")
            print_info("Activate it with: pvecli sdn apply")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@vnet_app.command("remove")
@async_to_sync
async def sdn_vnet_remove(
    vnet: str = typer.Argument(None, help="Vnet to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Remove a vnet. Refused while guests are still attached to it."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            names = sorted(v.get("vnet", "") for v in await client.get_sdn_vnets() or [])
            if not names:
                print_info("No SDN vnet defined")
                return
            if vnet is None:
                idx = select_menu(names, "  Vnet to remove:")
                if idx is None:
                    print_cancelled()
                    return
                vnet = names[idx]
            elif vnet not in names:
                print_error(f"Vnet '{vnet}' not found")
                raise typer.Exit(1)

            # Guests still bridged on it would lose their network on apply
            attached = []
            for res in await client.get_cluster_resources(resource_type="vm") or []:
                node, vmid = res.get("node"), res.get("vmid")
                if not node or vmid is None:
                    continue
                try:
                    cfg = (
                        await client.get_vm_config(node, vmid)
                        if res.get("type") == "qemu"
                        else await client.get_container_config(node, vmid=vmid)
                    )
                except PVECliError:
                    continue
                for key, value in (cfg or {}).items():
                    if key.startswith("net") and _uses_vnet(value, vnet):
                        attached.append(f"{vmid} ({res.get('name', '')})")
            if attached:
                print_error(
                    f"Vnet '{vnet}' is still used by: {', '.join(attached)}. "
                    "Move those guests first."
                )
                raise typer.Exit(1)

            # PVE refuses to drop a vnet that still owns subnets, and its raw
            # error does not say which ones.
            subnets = await client.get_sdn_subnets(vnet) or []
            if subnets:
                listed = ", ".join(
                    str(s.get("cidr") or s.get("subnet")) for s in subnets
                )
                print_error(
                    f"Vnet '{vnet}' still owns subnet(s): {listed}. "
                    f"Detach them first with: pvecli sdn subnet remove <cidr> --vnet {vnet}"
                )
                raise typer.Exit(1)

            if not yes and not confirm(f"Remove vnet '{vnet}'?", default=False):
                print_cancelled()
                return

            await client.delete_sdn_vnet(vnet)
            print_success(f"Vnet '{vnet}' removed (pending)")
            print_info("Apply the change with: pvecli sdn apply")

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@subnet_app.command("remove")
@async_to_sync
async def sdn_subnet_remove(
    cidr: str = typer.Argument(None, help="Subnet to detach, in CIDR form"),
    vnet: str = typer.Option(None, "--vnet", "-v", help="Vnet the subnet belongs to"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Detach a subnet from a vnet. A vnet cannot be removed while it has one."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            names = sorted(v.get("vnet", "") for v in await client.get_sdn_vnets() or [])
            if not names:
                print_info("No SDN vnet defined")
                return

            if vnet is None:
                idx = select_menu(names, "  Vnet:")
                if idx is None:
                    print_cancelled()
                    return
                vnet = names[idx]
            elif vnet not in names:
                print_error(f"Vnet '{vnet}' not found. Available: {', '.join(names)}")
                raise typer.Exit(1)

            subnets = await client.get_sdn_subnets(vnet) or []
            if not subnets:
                print_info(f"Vnet '{vnet}' has no subnet")
                return

            # The API id is not the CIDR: it is zone-a-b-c-d-mask
            by_cidr = {
                (s.get("cidr") or s.get("subnet", "")): s.get("subnet", "") for s in subnets
            }
            if cidr is None:
                choices = sorted(by_cidr)
                idx = select_menu(choices, "  Subnet to detach:")
                if idx is None:
                    print_cancelled()
                    return
                cidr = choices[idx]
            elif cidr not in by_cidr:
                print_error(
                    f"Subnet '{cidr}' not found on '{vnet}'. Available: "
                    f"{', '.join(sorted(by_cidr))}"
                )
                raise typer.Exit(1)

            if not yes and not confirm(
                f"Detach subnet '{cidr}' from vnet '{vnet}'?", default=False
            ):
                print_cancelled()
                return

            await client.delete_sdn_subnet(vnet, by_cidr[cidr])
            print_success(f"Subnet '{cidr}' detached from '{vnet}' (pending)")
            print_info("Apply the change with: pvecli sdn apply")

    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)


@subnet_app.command("add")
@async_to_sync
async def sdn_subnet_add(
    cidr: str = typer.Argument(None, help="Subnet in CIDR form, e.g. 10.0.30.0/24"),
    vnet: str = typer.Option(None, "--vnet", "-v", help="Vnet to attach the subnet to"),
    gateway: str = typer.Option(None, "--gateway", "-g", help="Gateway address"),
    snat: bool = typer.Option(False, "--snat", is_flag=True, help="Masquerade outgoing traffic"),
    dhcp_range: str = typer.Option(None, "--dhcp-range", help="DHCP range as start-ip:end-ip"),
    dhcp_dns: str = typer.Option(None, "--dhcp-dns", help="DNS server handed out by DHCP"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Attach a subnet to a vnet, optionally with a gateway, SNAT and DHCP."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        _require_write(profile_config, profile)

        async with ProxmoxClient(profile_config) as client:
            # Pending view: a vnet or zone created a moment ago and not applied
            # yet must count, its fields then live under 'pending'
            vnets_cfg = await client.get_sdn_vnets(pending=True) or []
            names = sorted(v.get("vnet", "") for v in vnets_cfg)
            if not names:
                print_error("No SDN vnet defined yet")
                raise typer.Exit(1)

            if vnet is None:
                console.print("\n[bold cyan]═══ Add SDN subnet ═══[/bold cyan]\n")
                idx = select_menu(names, "  Vnet:")
                if idx is None:
                    print_cancelled()
                    return
                vnet = names[idx]
            elif vnet not in names:
                print_error(f"Vnet '{vnet}' not found. Available: {', '.join(names)}")
                raise typer.Exit(1)

            if cidr is None:
                cidr = menu_prompt("  Subnet (CIDR)")
            if not cidr or "/" not in cidr:
                print_error("A subnet must be given in CIDR form, e.g. 10.0.30.0/24")
                raise typer.Exit(1)

            params: dict = {}
            if gateway:
                params["gateway"] = gateway
            if snat:
                params["snat"] = 1
            if dhcp_range:
                if ":" not in dhcp_range:
                    print_error("A DHCP range is given as start-ip:end-ip")
                    raise typer.Exit(1)
                start, end = dhcp_range.split(":", 1)
                params["dhcp-range"] = f"start-address={start},end-address={end}"
            if dhcp_dns:
                params["dhcp-dns-server"] = dhcp_dns

            console.print("\n[bold cyan]═══ Summary ═══[/bold cyan]\n")
            console.print(f"  [bold]Subnet:[/bold] {cidr}")
            for key, value in params.items():
                console.print(f"  [bold]{key}:[/bold] {value}")
            if dhcp_range:
                print_warning(
                    "DHCP is a technology preview. Every node needs dnsmasq installed, "
                    "and the stock dnsmasq service disabled, otherwise it holds port 53 "
                    "and the per-zone instance cannot start:"
                )
                console.print(
                    "  [dim]apt install dnsmasq && systemctl disable --now dnsmasq[/dim]"
                )
                vnet_cfg = next((v for v in vnets_cfg if v.get("vnet") == vnet), {})
                zone_name = vnet_cfg.get("zone") or (vnet_cfg.get("pending") or {}).get("zone")
                zone_cfg = next(
                    (z for z in await client.get_sdn_zones(pending=True) or []
                     if z.get("zone") == zone_name),
                    {},
                )
                if not (zone_cfg.get("dhcp") or (zone_cfg.get("pending") or {}).get("dhcp")):
                    print_warning(
                        f"Zone '{zone_name}' has no DHCP enabled: the range stays unused "
                        f"until it does (pvecli sdn zone set {zone_name} --dhcp)"
                    )

            if not yes and not confirm("\nCreate this subnet?", default=True):
                print_cancelled()
                return

            await client.create_sdn_subnet(vnet, cidr, **params)
            print_success(f"Subnet '{cidr}' attached to '{vnet}' (pending)")
            print_info("Activate it with: pvecli sdn apply")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(_explain_sdn_error(e))
        raise typer.Exit(1)
