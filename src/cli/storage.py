"""Storage management commands."""

import asyncio
import os
import re
from getpass import getpass
from pathlib import Path
from typing import Any, cast

import typer
from rich.panel import Panel
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    build_ordered_table,
    confirm,
    console,
    emit_json,
    format_bytes,
    format_percentage,
    menu_prompt,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
    prompt,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import multi_select_menu, select_menu
from ._shared import pick_node

app = typer.Typer(help="Manage storage", no_args_is_help=True, cls=ordered_group(["add", "remove", "config", "content", "list", "show"]))
content_app = typer.Typer(help="Manage storage content", no_args_is_help=True)
app.add_typer(content_app, name="content")


async def _pick_storage(client: ProxmoxClient, node: str) -> str | None:
    """Interactive single-select for a storage on a node. Returns storage id or None."""
    storages = await client.get_storage_list(node)
    storage_ids = sorted(s.get("storage", "") for s in storages if s.get("storage"))
    if not storage_ids:
        print_info(f"No storages found on node '{node}'")
        return None
    idx = select_menu(storage_ids, f"  Select storage on '{node}':")
    if idx is None:
        print_cancelled()
        return None
    return storage_ids[idx]


async def _resolve_node_storage(client: ProxmoxClient, node: str | None, storage: str | None) -> tuple[str, str] | None:
    """Resolve node and storage interactively if not provided. Returns (node, storage) or None on cancel."""
    if not node:
        node = await pick_node(client)
        if node is None:
            return None
    if not storage:
        storage = await _pick_storage(client, node)
        if storage is None:
            return None
    return node, storage


# ── storage add ──────────────────────────────────────────────────────────

# Shared storage types pvecli can create.
#   scan / scan_arg: discovery endpoint under /nodes/{node}/scan, verified
#     against the published API schema. None when the backend cannot be probed.
#   supported: content types the backend accepts, from the "Available storage
#     types" table of the Proxmox storage documentation.
#   default: the subset preselected in the wizard.
# All of these types are shared by nature, so the 'shared' property is never
# sent: PVE rejects it for them.
_FILE_CONTENT = ("images", "rootdir", "vztmpl", "iso", "backup", "snippets", "import")

# Same rule as PVE's own storage ID check (pve_verify_storage_id), so a bad
# ID is refused before any scan instead of after the summary.
_STORAGE_ID_RE = re.compile(r"^[a-z][a-z0-9\-_.]*[a-z0-9]$", re.IGNORECASE)

# A self-signed PBS rejects the scan and names the fingerprint it presents.
_FINGERPRINT_RE = re.compile(r"(?:[0-9a-f]{2}:){31}[0-9a-f]{2}", re.IGNORECASE)

_STORAGE_TYPES: dict[str, dict] = {
    "nfs": {
        "label": "NFS share",
        "scan": "nfs",
        "scan_arg": "server",
        "supported": _FILE_CONTENT,
        "default": "images,rootdir,vztmpl,iso,backup",
    },
    "cifs": {
        "label": "SMB/CIFS share",
        "scan": "cifs",
        "scan_arg": "server",
        "supported": _FILE_CONTENT,
        "default": "images,rootdir,vztmpl,iso,backup",
    },
    "rbd": {
        "label": "Ceph RBD (external cluster)",
        "scan": None,
        "supported": ("images", "rootdir"),
        "default": "images,rootdir",
    },
    "cephfs": {
        "label": "CephFS (external cluster)",
        "scan": None,
        "supported": ("vztmpl", "iso", "backup", "snippets", "import"),
        "default": "vztmpl,iso,backup",
    },
    "pbs": {
        "label": "Proxmox Backup Server",
        "scan": "pbs",
        "scan_arg": "server",
        # /nodes/{node}/scan/pbs also requires username and password:
        # without them the datastore cannot be discovered, only declared.
        "scan_needs": ("username", "password"),
        "supported": ("backup",),
        "default": "backup",
    },
    "iscsi": {
        "label": "iSCSI target",
        "scan": "iscsi",
        "scan_arg": "portal",
        # The iSCSI plugin advertises images, or none to only expose the LUNs
        # as a base for an LVM storage
        "supported": ("images", "none"),
        "default": "images",
    },
}


async def _probe_from_all_nodes(
    client: ProxmoxClient, nodes: list[str], scan_type: str, **params: Any
) -> dict[str, list | str]:
    """Run the same scan from every node at once. Returns {node: entries or error}.

    Concurrent on purpose: an unreachable server makes each scan wait for its
    own timeout, which would otherwise add up node after node.
    """

    async def one(node: str) -> list | str:
        try:
            return await client.scan_storage(node, scan_type, **params)
        except PVECliError as e:
            return str(e)

    results = await asyncio.gather(*(one(n) for n in nodes))
    return dict(zip(nodes, results, strict=True))


def _keyring_secret(keyring: str, client: str) -> str:
    """The bare key of [client.<client>] in a keyring file.

    A keyring may hold several clients (admin, then pve...): taking the
    first key would mount CephFS with another client's secret. Falls back
    on the only key when the file has a single one.
    """
    sections: dict[str, str] = {}
    current = ""
    for line in keyring.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
        elif line.startswith("key") and "=" in line:
            sections[current] = line.split("=", 1)[1].strip()
    wanted = client if client.startswith("client.") else f"client.{client}"
    if wanted in sections:
        return sections[wanted]
    if len(sections) == 1:
        return next(iter(sections.values()))
    raise PVECliError(
        f"The keyring has no [{wanted}] section: {', '.join(sorted(sections)) or 'no key at all'}"
    )


def _scan_key(scan_type: str, entry: dict) -> str:
    """Identifier of a discovered entry, per backend."""
    if scan_type == "nfs":
        return str(entry.get("path", ""))
    if scan_type == "cifs":
        return str(entry.get("share", ""))
    if scan_type == "iscsi":
        return str(entry.get("target", ""))
    if scan_type == "pbs":
        return str(entry.get("store", ""))
    return str(entry.get("pool") or entry.get("vg") or entry.get("name") or "")


@app.command("add")
@async_to_sync
async def add_storage(
    storage_id: str = typer.Argument(None, help="Storage ID to create"),
    storage_type: str = typer.Option(None, "--type", "-t", help=f"Type: {', '.join(_STORAGE_TYPES)}"),
    server: str = typer.Option(None, "--server", help="Server for nfs, cifs, pbs"),
    export: str = typer.Option(None, "--export", help="NFS export path"),
    share: str = typer.Option(None, "--share", help="CIFS share name"),
    portal: str = typer.Option(None, "--portal", help="iSCSI portal"),
    target: str = typer.Option(None, "--target", help="iSCSI target"),
    datastore: str = typer.Option(None, "--datastore", help="PBS datastore"),
    fingerprint: str = typer.Option(None, "--fingerprint", help="TLS fingerprint of the PBS server (self-signed certificates)"),
    monhost: str = typer.Option(None, "--monhost", help="Ceph monitors, comma separated"),
    ceph_pool: str = typer.Option(None, "--pool", help="Ceph RBD pool"),
    fs_name: str = typer.Option(None, "--fs-name", help="CephFS filesystem name"),
    username: str = typer.Option(None, "--username", help="User name: Ceph client id (default admin), CIFS user, or PBS user"),
    keyring_file: str = typer.Option(None, "--keyring-file", help="Path to the Ceph keyring file (never pass the key on the command line)"),
    password_env: str = typer.Option(None, "--password-env", help="Env var holding the CIFS or PBS password"),
    content: str = typer.Option(None, "--content", "-c", help="Content types, comma separated"),
    nodes: str = typer.Option(None, "--nodes", "-n", help="Restrict to these nodes, comma separated"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Add a shared storage to the cluster, checking every node can reach it."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            all_nodes = sorted(
                n.get("node", "")
                for n in await client.get_nodes()
                if n.get("status") == "online"
            )
            if not all_nodes:
                print_error("No online node found")
                raise typer.Exit(1)

            if nodes:
                asked = [n.strip() for n in nodes.split(",") if n.strip()]
                unknown = [n for n in asked if n not in all_nodes]
                if unknown:
                    print_error(
                        f"Unknown node(s): {', '.join(unknown)}. "
                        f"Cluster members: {', '.join(all_nodes)}"
                    )
                    raise typer.Exit(1)

            interactive = storage_type is None

            # ── Type ──
            if storage_type is None:
                console.print("\n[bold cyan]═══ Add Storage ═══[/bold cyan]\n")
                keys = list(_STORAGE_TYPES)
                items = [f"{k.ljust(8)} {_STORAGE_TYPES[k]['label']}" for k in keys]
                idx = select_menu(items, "  Storage type:")
                if idx is None:
                    print_cancelled()
                    return
                storage_type = keys[idx]
            elif storage_type not in _STORAGE_TYPES:
                print_error(
                    f"Unsupported type '{storage_type}'. Available: {', '.join(_STORAGE_TYPES)}"
                )
                raise typer.Exit(1)

            spec = _STORAGE_TYPES[storage_type]

            # ── Pure input checks first, before touching the network ──
            if content is not None:
                asked = [c.strip() for c in content.split(",") if c.strip()]
                unsupported = [c for c in asked if c not in spec["supported"]]
                if unsupported:
                    print_error(
                        f"Content type(s) {', '.join(unsupported)} are not supported by "
                        f"'{storage_type}'. Supported: {', '.join(spec['supported'])}"
                    )
                    raise typer.Exit(1)
                if not asked:
                    print_error("At least one content type is required")
                    raise typer.Exit(1)

            # ── Existing storages: refuse duplicates early ──
            existing = {s.get("storage") for s in await client.get_storage_configs()}

            if storage_id is None:
                storage_id = menu_prompt("  Storage ID")
            if not storage_id:
                print_cancelled()
                return
            if not _STORAGE_ID_RE.match(storage_id):
                print_error(
                    f"Invalid storage ID '{storage_id}': at least 2 characters, starting "
                    "with a letter, ending with a letter or digit, and only letters, "
                    "digits, '-', '_' or '.' in between"
                )
                raise typer.Exit(1)
            if storage_id in existing:
                print_error(f"Storage '{storage_id}' already exists")
                raise typer.Exit(1)

            params: dict[str, Any] = {}

            # ── Backend specific parameters, with discovery when possible ──
            if storage_type in ("nfs", "cifs", "pbs"):
                if server is None:
                    server = menu_prompt("  Server (IP or DNS name)")
                if not server:
                    print_cancelled()
                    return
                params["server"] = server

            if storage_type == "iscsi":
                if portal is None:
                    portal = menu_prompt("  Portal (IP or DNS name)")
                if not portal:
                    print_cancelled()
                    return
                params["portal"] = portal

            # Discovery from every node: this is what the web UI never does
            probe_arg = spec.get("scan_arg")
            chosen_entry = {"nfs": export, "cifs": share, "iscsi": target, "pbs": datastore}.get(
                storage_type
            )
            # PBS and CIFS discovery need credentials: a share without guest
            # access answers ACCESS_DENIED to an anonymous scan, from every
            # node. Collect them before deciding whether the scan can run.
            if storage_type == "cifs":
                if username:
                    params["username"] = username
                if password_env:
                    pwd = os.environ.get(password_env)
                    if not pwd:
                        print_error(f"Environment variable '{password_env}' is empty or unset")
                        raise typer.Exit(1)
                    params["password"] = pwd
            if storage_type == "pbs":
                if fingerprint:
                    params["fingerprint"] = fingerprint
                if username:
                    params["username"] = username
                if password_env:
                    pwd = os.environ.get(password_env)
                    if not pwd:
                        print_error(f"Environment variable '{password_env}' is empty or unset")
                        raise typer.Exit(1)
                    params["password"] = pwd
                elif interactive and params.get("username"):
                    params["password"] = getpass("  PBS password: ")

            # A self-signed PBS refuses the scan without its fingerprint, and
            # says which one it expects in the error.
            scan_needs = spec.get("scan_needs", ())
            missing_for_scan = [k for k in scan_needs if not params.get(k)]
            if spec["scan"] and probe_arg and missing_for_scan:
                print_info(
                    f"Discovery skipped: scanning {storage_type} also requires "
                    f"{', '.join(missing_for_scan)}"
                )
            elif spec["scan"] and probe_arg:
                probe_value = params.get(probe_arg)
                console.print(
                    f"\n[cyan]Scanning {storage_type} on {probe_value} from {len(all_nodes)} node(s)...[/cyan]"
                )
                # The scan needs whatever the backend requires to authenticate,
                # not only the server address: PBS wants username, password and
                # the fingerprint of its self-signed certificate.
                scan_args = {probe_arg: probe_value}
                for key in ("username", "password", "fingerprint", "domain"):
                    if params.get(key):
                        scan_args[key] = params[key]
                probes = await _probe_from_all_nodes(
                    client, all_nodes, spec["scan"], **scan_args
                )

                ok_nodes = {n: r for n, r in probes.items() if isinstance(r, list)}
                ko_nodes = {n: r for n, r in probes.items() if not isinstance(r, list)}

                fingerprints = {
                    fp.lower()
                    for err in ko_nodes.values()
                    for fp in _FINGERPRINT_RE.findall(str(err))
                }
                if not ok_nodes and storage_type == "pbs" and len(fingerprints) == 1:
                    # Reachable, but its self-signed certificate is not trusted yet
                    print_error(
                        f"{probe_value} answered, but its TLS certificate is not trusted. "
                        "If this fingerprint is the one shown on the PBS dashboard, "
                        "run the command again with:"
                    )
                    console.print(f"  --fingerprint {fingerprints.pop()}", soft_wrap=True)
                    raise typer.Exit(1)

                if not ok_nodes:
                    print_error(f"No node could reach {probe_value}:")
                    for n, err in ko_nodes.items():
                        console.print(f"  [red]{n}[/red]: {err}")
                    raise typer.Exit(1)

                # Which entries are visible from every node
                per_node_keys = {
                    n: {_scan_key(spec["scan"], e) for e in entries}
                    for n, entries in ok_nodes.items()
                }
                common = set.intersection(*per_node_keys.values()) if per_node_keys else set()

                table = Table(title="Backend visibility", show_header=True, header_style="bold cyan")
                table.add_column("Node", style="cyan")
                table.add_column("Reachable")
                table.add_column("Entries", justify="right")
                for n in all_nodes:
                    r = probes.get(n)
                    if isinstance(r, list):
                        table.add_row(n, "[green]yes[/green]", str(len(r)))
                    else:
                        table.add_row(n, "[red]no[/red]", "-")
                console.print(table)

                if ko_nodes:
                    print_warning(
                        f"{len(ko_nodes)} node(s) cannot reach this backend: "
                        + ", ".join(ko_nodes)
                    )
                    if nodes is None and not yes:
                        if confirm(
                            "Restrict the storage to the nodes that can reach it?",
                            default=True,
                        ):
                            nodes = ",".join(sorted(ok_nodes))

                # Entry selection among what was discovered
                all_keys = sorted(set().union(*per_node_keys.values()))
                if chosen_entry is not None:
                    if chosen_entry not in all_keys:
                        print_warning(
                            f"'{chosen_entry}' was not found by the scan, creating anyway"
                        )
                elif not all_keys:
                    print_error("The backend exposes nothing usable")
                    raise typer.Exit(1)
                elif len(all_keys) == 1 and all_keys[0] in common:
                    # Nothing to choose: the only entry, seen by every node
                    chosen_entry = all_keys[0]
                    print_info(f"Using the only {storage_type} entry found: {chosen_entry}")
                elif yes:
                    # --yes means unattended: never fall into a menu
                    entry_flag = {
                        "nfs": "--export", "cifs": "--share", "iscsi": "--target", "pbs": "--datastore",
                    }[storage_type]
                    print_error(
                        f"Several {storage_type} entries found, pick one with "
                        f"{entry_flag}: {', '.join(all_keys)}"
                    )
                    raise typer.Exit(1)
                else:
                    items = [
                        k + ("" if k in common else "   [seen on some nodes only]")
                        for k in all_keys
                    ]
                    idx = select_menu(items, f"  {storage_type} entry:")
                    if idx is None:
                        print_cancelled()
                        return
                    chosen_entry = all_keys[idx]

            # The chosen entry is applied whether it came from discovery or
            # straight from a flag: skipping the scan must not drop it.
            entry_param = {
                "nfs": "export",
                "cifs": "share",
                "iscsi": "target",
                "pbs": "datastore",
            }.get(storage_type)
            if entry_param:
                if not chosen_entry:
                    chosen_entry = menu_prompt(f"  {entry_param}")
                if not chosen_entry:
                    print_error(f"'{entry_param}' is required for a {storage_type} storage")
                    raise typer.Exit(1)
                params[entry_param] = chosen_entry

            # ── External Ceph: no scan endpoint, everything is declarative ──
            if storage_type in ("rbd", "cephfs"):
                if monhost is None:
                    monhost = menu_prompt("  Ceph monitors (comma separated)")
                if not monhost:
                    print_cancelled()
                    return
                params["monhost"] = monhost
                params["username"] = username or (
                    menu_prompt("  Ceph client id", default="admin") if interactive else "admin"
                )
                if storage_type == "rbd":
                    if ceph_pool is None and interactive:
                        ceph_pool = menu_prompt("  RBD pool", default="rbd")
                    params["pool"] = ceph_pool or "rbd"
                else:
                    if fs_name is None and interactive:
                        fs_name = menu_prompt("  CephFS filesystem name", default="cephfs")
                    params["fs-name"] = fs_name or "cephfs"

                if keyring_file is None and interactive:
                    keyring_file = menu_prompt("  Path to the keyring file")
                if keyring_file:
                    kf = Path(keyring_file).expanduser()
                    if not kf.is_file():
                        print_error(f"Keyring file not found: {kf}")
                        raise typer.Exit(1)
                    raw_key = kf.read_text().strip()
                    # rbd stores the whole keyring, cephfs wants the bare key.
                    # Accept either file shape and hand each backend what it
                    # expects, instead of failing on a silent mount error.
                    if storage_type == "cephfs" and "key" in raw_key and "=" in raw_key:
                        raw_key = _keyring_secret(raw_key, params["username"])
                    params["keyring"] = raw_key
                else:
                    print_warning(
                        "No keyring provided: the storage will be created but stay inactive "
                        "until the key is in place"
                    )

            # ── Credentials from the environment, never from argv ──
            if password_env:
                pwd = os.environ.get(password_env)
                if not pwd:
                    print_error(f"Environment variable '{password_env}' is empty or unset")
                    raise typer.Exit(1)
                params["password"] = pwd
            if storage_type == "cifs":
                # --username must reach the payload whatever the password came
                # from: a CIFS share created with a password but no user name
                # mounts nowhere, and the node report only says "error".
                if username:
                    params.setdefault("username", username)
                if "password" not in params:
                    if interactive and confirm("Does this share need a password?", default=False):
                        params["password"] = getpass("  CIFS password: ")
                        params["username"] = username or menu_prompt("  CIFS username")

            # ── Content types: only offer, and only accept, what this
            # backend actually supports ──
            choices = list(spec["supported"])
            if content is None:
                if interactive:
                    preselected = [
                        i for i, c in enumerate(choices) if c in spec["default"].split(",")
                    ]
                    sel = multi_select_menu(
                        choices,
                        "  Content types (Space to toggle, Enter to confirm):",
                        preselected=preselected,
                    )
                    if sel is None:
                        print_cancelled()
                        return
                    content = ",".join(choices[i] for i in sel)
                else:
                    content = spec["default"]
            params["content"] = content

            if nodes:
                params["nodes"] = nodes
            # No 'shared' parameter here: nfs, cifs, rbd, cephfs, pbs and iscsi
            # are shared by nature and PVE rejects the property for them.

            # ── Summary ──
            console.print("\n[bold cyan]═══ Summary ═══[/bold cyan]\n")
            console.print(f"  [bold]Storage:[/bold] {storage_id}")
            console.print(f"  [bold]Type:[/bold]    {storage_type} ({spec['label']})")
            for key, value in params.items():
                if key in ("keyring", "password"):
                    console.print(f"  [bold]{key}:[/bold] [dim](provided, hidden)[/dim]")
                else:
                    console.print(f"  [bold]{key}:[/bold] {value}")
            console.print(
                f"  [bold]Applies to:[/bold] "
                f"{nodes or (all_nodes[0] if len(all_nodes) == 1 else 'every node in the cluster')}"
            )

            if not yes and not confirm("\nCreate this storage?", default=True):
                print_cancelled()
                return

            await client.create_storage(storage_id, storage_type, **params)
            print_success(f"Storage '{storage_id}' created")

            # ── Verification: does it actually work on each node ──
            target_nodes = [n.strip() for n in nodes.split(",")] if nodes else all_nodes
            console.print("\n[cyan]Checking the storage on each node...[/cyan]")

            # Mounting a remote share cluster wide takes longer than any fixed
            # delay would cover, so poll until every node reports it active.
            statuses: dict[str, dict | str] = {}
            for _ in range(10):
                statuses = {}
                for node in target_nodes:
                    try:
                        statuses[node] = await client.get_storage_status(node, storage_id)
                    except PVECliError as e:
                        statuses[node] = str(e)
                if all(
                    isinstance(v, dict) and v.get("active") for v in statuses.values()
                ):
                    break
                await asyncio.sleep(3)

            table = Table(title=f"Status of '{storage_id}'", show_header=True, header_style="bold cyan")
            table.add_column("Node", style="cyan")
            table.add_column("Active")
            table.add_column("Total", justify="right")
            table.add_column("Available", justify="right")
            failed = []
            for node in target_nodes:
                st = statuses.get(node)
                if not isinstance(st, dict):
                    table.add_row(node, "[red]error[/red]", "-", "-")
                    failed.append(f"{node} ({st})")
                    continue
                active = st.get("active", 0)
                table.add_row(
                    node,
                    "[green]yes[/green]" if active else "[red]no[/red]",
                    format_bytes(st.get("total", 0)) if st.get("total") else "-",
                    format_bytes(st.get("avail", 0)) if st.get("avail") else "-",
                )
                if not active:
                    failed.append(node)
            console.print(table)

            if len(failed) == len(target_nodes):
                print_error(
                    f"Storage '{storage_id}' was created but is active on no node. "
                    "Check the backend, or remove it with: "
                    f"pvecli storage remove {storage_id}"
                )
                raise typer.Exit(1)
            if failed:
                print_warning(
                    "Storage created but not active on: " + ", ".join(str(f) for f in failed)
                )
            else:
                print_success(f"Storage '{storage_id}' is active on all {len(target_nodes)} node(s)")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("remove")
@async_to_sync
async def remove_storage(
    storage_id: str = typer.Argument(None, help="Storage ID to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Remove a storage definition. The data on the backend is left untouched."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            configs = await client.get_storage_configs()
            ids = sorted(s.get("storage", "") for s in configs if s.get("storage"))

            if storage_id is None:
                idx = select_menu(ids, "  Storage to remove:")
                if idx is None:
                    print_cancelled()
                    return
                storage_id = ids[idx]
            elif storage_id not in ids:
                print_error(f"Storage '{storage_id}' not found")
                raise typer.Exit(1)

            cfg = next((s for s in configs if s.get("storage") == storage_id), {})
            console.print(f"\n  [bold]Storage:[/bold] {storage_id}")
            console.print(f"  [bold]Type:[/bold]    {cfg.get('type', '?')}")
            console.print(f"  [bold]Content:[/bold] {cfg.get('content', '-')}")
            print_info("Only the PVE definition is removed, data on the backend is kept")

            if not yes and not confirm(
                f"Remove storage '{storage_id}' from the cluster configuration?",
                default=False,
            ):
                print_cancelled()
                return

            await client.delete_storage(storage_id)
            print_success(f"Storage '{storage_id}' removed")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── storage list ─────────────────────────────────────────────────────────


@app.command("list")
@fleet(parallel=True)
@async_to_sync
async def list_storage(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    node: str = typer.Option(None, "--node", "-n", help="Show storage for specific node"),
    order: str = typer.Option(None, "--order", "-o", help="Sort by column (moved to first position), e.g. storage, type, usage"),
    json_output: bool = JSON_OPTION,
) -> None:
    """List all storage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        profile_name = profile or config_manager.get().default_profile

        async with ProxmoxClient(profile_config) as client:
            if node:
                storage_list = await client.get_storage_list(node)
                title = f"Storage on {node}"
            else:
                nodes = await client.get_nodes()
                storage_list = []
                for n in nodes:
                    node_storage = await client.get_storage_list(cast(str, n.get("node")))
                    for s in node_storage:
                        s["_node"] = n.get("node")
                    storage_list.extend(node_storage)
                title = "Cluster Storage"

            if json_output:
                # Raw /storage entries, the internal "_node" key exposed as "node"
                # and the usage ratio as a real percentage (0.0 when total is 0).
                payload = []
                for storage in storage_list:
                    item = {k: v for k, v in storage.items() if k != "_node"}
                    item["node"] = storage.get("_node") or node
                    total = storage.get("total") or 0
                    used = storage.get("used") or 0
                    item["used_percent"] = (used / total * 100) if total else 0.0
                    payload.append(item)
                emit_json({"profile": profile_name, "node": node, "storages": payload})
                return

            if not storage_list:
                print_info("No storage found")
                return

            columns = []
            if not node:
                columns.append(("Node", {"style": "cyan"}))
            columns.extend([
                ("Storage", {"style": "cyan"}),
                ("Type", {}),
                ("Content", {}),
                ("Status", {}),
                ("Total", {"justify": "right"}),
                ("Used", {"justify": "right"}),
                ("Available", {"justify": "right"}),
                ("Usage %", {"justify": "right"}),
            ])

            rows = []
            for storage in storage_list:
                active = storage.get("active", False)
                enabled = storage.get("enabled", True)
                if active and enabled:
                    status_val, status = "active", "[green]active[/green]"
                elif enabled:
                    status_val, status = "inactive", "[yellow]inactive[/yellow]"
                else:
                    status_val, status = "disabled", "[red]disabled[/red]"

                total = storage.get("total", 0)
                used = storage.get("used", 0)
                avail = storage.get("avail", 0)
                used_pct = (used / total * 100) if total else -1.0

                row = {
                    "Storage": (storage.get("storage", "-"), storage.get("storage", "-")),
                    "Type": (storage.get("type", "-"), storage.get("type", "-")),
                    "Content": (storage.get("content", "-"), storage.get("content", "-")),
                    "Status": (status_val, status),
                    "Total": (total, format_bytes(total) if total else "-"),
                    "Used": (used, format_bytes(used) if total else "-"),
                    "Available": (avail, format_bytes(avail) if total else "-"),
                    "Usage %": (used_pct, format_percentage(used_pct) if total else "-"),
                }
                if not node:
                    row["Node"] = (storage.get("_node", "-"), storage.get("_node", "-"))
                rows.append(row)

            table = build_ordered_table(title, columns, rows, order)
            if table is None:
                raise typer.Exit(1)
            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── storage show ─────────────────────────────────────────────────────────


def _render_storage_panel(node: str, storage: str, status: dict, config: dict) -> Panel:
    """Build a Rich Panel for a single storage."""
    lines = []
    lines.append("[bold]── General ──[/bold]")
    lines.append(f"[bold]Storage:[/bold]    {storage}")
    lines.append(f"[bold]Node:[/bold]       {node}")
    lines.append(f"[bold]Type:[/bold]       {status.get('type', 'unknown')}")

    active = status.get("active", False)
    enabled = status.get("enabled", True)
    if active and enabled:
        status_str = "[green]active[/green]"
    elif enabled:
        status_str = "[yellow]inactive[/yellow]"
    else:
        status_str = "[red]disabled[/red]"
    lines.append(f"[bold]Status:[/bold]     {status_str}")

    lines.append(f"[bold]Content:[/bold]    {status.get('content', '-')}")
    lines.append(f"[bold]Shared:[/bold]     {'Yes' if status.get('shared') else 'No'}")

    if 'path' in config:
        lines.append(f"[bold]Path:[/bold]       {config.get('path')}")

    total = status.get("total", 0)
    used = status.get("used", 0)
    avail = status.get("avail", 0)

    if total:
        lines.append("")
        lines.append("[bold]── Capacity ──[/bold]")
        used_pct = (used / total * 100) if total else 0
        lines.append(f"[bold]Total:[/bold]      {format_bytes(total)}")
        lines.append(f"[bold]Used:[/bold]       {format_bytes(used)} ({format_percentage(used_pct)})")
        lines.append(f"[bold]Available:[/bold]  {format_bytes(avail)}")

    return Panel("\n".join(lines), title=f"Storage: {storage}", border_style="blue")


@app.command("info")
@async_to_sync
async def show_storage(
    node: str | None = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Show detailed storage information and configuration."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if not node:
                node = await pick_node(client)
                if node is None:
                    return

            if not storage:
                storages = await client.get_storage_list(node)
                storage_ids = sorted(s.get("storage", "") for s in storages if s.get("storage"))
                if not storage_ids:
                    print_info(f"No storages found on node '{node}'")
                    return
                sel = multi_select_menu(storage_ids, f"  Select storage(s) on '{node}':")
                if sel is None:
                    print_cancelled()
                    return
                if not sel:
                    print_cancelled()
                    return
                selected_storages = [storage_ids[i] for i in sel]
            else:
                selected_storages = [storage]

            for sid in selected_storages:
                status = await client.get_storage_status(node, sid)
                config = await client.get_storage_config(sid)
                console.print(_render_storage_panel(node, sid, status, config))

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── storage config ───────────────────────────────────────────────────────


_CONTENT_TYPES = [
    "images", "rootdir", "vztmpl", "backup", "iso", "snippets", "import",
]


@app.command("config")
@async_to_sync
async def config_storage(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Configure storage content types interactively."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            config = await client.get_storage_config(storage)
            storage_type = config.get('type', 'unknown')

            console.print("\n[bold cyan]═══ Edit Storage ═══[/bold cyan]\n")
            console.print(f"[bold cyan]Node:[/bold cyan]    {node}")
            console.print(f"[bold cyan]Storage:[/bold cyan] {storage}")
            console.print(f"[bold cyan]Type:[/bold cyan]    {storage_type}\n")

            current_content = config.get('content', '')
            current_types = [ct.strip() for ct in current_content.split(',') if ct.strip()]

            preselected = [i for i, name in enumerate(_CONTENT_TYPES) if name in current_types]

            sel = multi_select_menu(_CONTENT_TYPES, "  Content types (Space to toggle, Enter to confirm):", preselected=preselected)
            if sel is None:
                print_cancelled()
                return

            selected = [_CONTENT_TYPES[i] for i in sel]
            new_content = ",".join(selected)

            console.print("\n[bold cyan]Changes:[/bold cyan]")
            console.print(f"  [bold]Before:[/bold] [yellow]{current_content}[/yellow]")
            console.print(f"  [bold]After:[/bold]  [green]{new_content}[/green]")

            if not new_content:
                print_warning("No content types selected!")

            if not confirm("Apply these changes?", default=True):
                print_cancelled()
                return

            await client.update_storage_config(storage, content=new_content)
            print_success(f"Storage '{storage}' updated successfully")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print()
        print_cancelled()
        raise typer.Exit(0)


# ── storage content list / add / delete ──────────────────────────────────


@content_app.command("list")
@async_to_sync
async def list_content(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    content_type: str = typer.Option(None, "--type", "-t", help="Filter by content type"),
) -> None:
    """List storage content."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            content = await client.get_storage_content(node, storage, content_type)

            if not content:
                print_info(f"No content found in storage '{storage}'")
                return

            table = Table(
                title=f"Content in {storage} on {node}",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Volume ID", style="cyan")
            table.add_column("Content")
            table.add_column("Format")
            table.add_column("Size", justify="right")
            table.add_column("VMID", justify="right")

            for item in content:
                table.add_row(
                    item.get("volid", "-"),
                    item.get("content", "-"),
                    item.get("format", "-"),
                    format_bytes(item.get("size", 0)) if item.get("size") else "-",
                    str(item["vmid"]) if item.get("vmid") else "-",
                )

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@content_app.command("add")
@async_to_sync
async def add_content(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    source_file: str = typer.Option(None, "--source-file", "-s", help="Path to file to upload"),
    content_type: str = typer.Option(None, "--type", "-t", help="Content type: iso, vztmpl, or import"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Upload content to storage (ISO images, container templates, or import content)."""
    config_manager = ConfigManager()

    try:
        from pathlib import Path

        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            if not source_file:
                source_file = prompt("  File path")
                if not source_file.strip():
                    print_error("File path cannot be empty")
                    raise typer.Exit(1)
                source_file = source_file.strip()

            file = Path(source_file)
            if not file.exists():
                print_error(f"File not found: {source_file}")
                raise typer.Exit(1)

            valid_types = ["iso", "vztmpl", "import"]
            if not content_type:
                idx = select_menu(valid_types, "  Content type:")
                if idx is None:
                    print_cancelled()
                    return
                content_type = valid_types[idx]
            elif content_type not in valid_types:
                print_error(f"Invalid content type '{content_type}'. Valid: {', '.join(valid_types)}")
                raise typer.Exit(1)

            file_size = file.stat().st_size

            console.print("\n[bold]Upload details:[/bold]")
            console.print(f"  File:    {file.name}")
            console.print(f"  Size:    {format_bytes(file_size)}")
            console.print(f"  Storage: {storage}")
            console.print(f"  Node:    {node}")
            console.print(f"  Type:    {content_type}")

            if not yes and not confirm("Proceed with upload?", default=True):
                print_cancelled()
                return

            console.print("\n[cyan]Uploading...[/cyan]")

            try:
                upid = await client.upload_storage_content(
                    node=node,
                    storage=storage,
                    content_type=content_type,
                    file_path=str(file),
                )

                print_success("Upload started successfully")
                console.print(f"[cyan]Task ID:[/cyan] {upid}")

                from ..api.client import _upid_node
                exec_node = _upid_node(upid)
                if exec_node and exec_node != node:
                    print_info(
                        f"Task executed by '{exec_node}' (proxying node). "
                        f"Shared storage: the file lands in the same place."
                    )

                console.print("[cyan]Waiting for upload to complete...[/cyan]")
                task_result = await client.wait_for_task(node, upid)

                exitstatus = task_result.get("exitstatus", "")
                if exitstatus == "OK":
                    print_success(f"File '{file.name}' uploaded to '{storage}'")
                else:
                    print_error(f"Upload failed with status: {exitstatus}")
                    raise typer.Exit(1)

            except FileNotFoundError as e:
                print_error(str(e))
                raise typer.Exit(1)

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@content_app.command("download")
@async_to_sync
async def download_content(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    url: str = typer.Option(None, "--url", "-u", help="URL to download from"),
    filename: str = typer.Option(None, "--filename", "-f", help="Target filename (default: extracted from URL)"),
    content_type: str = typer.Option(None, "--type", "-t", help="Content type: iso or vztmpl"),
    checksum: str = typer.Option(None, "--checksum", "-c", help="Expected checksum"),
    checksum_algorithm: str = typer.Option(
        None, "--checksum-algorithm", "-ca", help="Checksum algorithm (md5, sha1, sha256, sha512)"
    ),
    background: bool = typer.Option(False, "--background", "-b", help="Start download and return immediately"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Download content from a URL directly to Proxmox storage."""
    from urllib.parse import unquote, urlparse

    from rich.progress import Progress, SpinnerColumn, TextColumn

    config_manager = ConfigManager()
    upid = None

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            if not url:
                url = prompt("  URL")
                if not url or not url.strip():
                    print_error("URL cannot be empty")
                    raise typer.Exit(1)
                url = url.strip()

            # Extract default filename from URL
            parsed = urlparse(url)
            url_filename = unquote(parsed.path.rsplit("/", 1)[-1]) if parsed.path else ""

            if not filename:
                if url_filename:
                    filename = prompt("  Filename", default=url_filename)
                else:
                    filename = prompt("  Filename")
                    if not filename or not filename.strip():
                        print_error("Filename cannot be empty")
                        raise typer.Exit(1)
                    filename = filename.strip()

            # Auto-detect content type from filename
            valid_types = ["iso", "vztmpl"]
            if not content_type:
                if filename.endswith((".iso", ".img")):
                    guessed = "iso"
                elif filename.endswith((".tar.gz", ".tar.xz", ".tar.zst")):
                    guessed = "vztmpl"
                else:
                    guessed = None

                if guessed:
                    console.print(f"  [dim]Detected content type: {guessed}[/dim]")
                    content_type = guessed
                else:
                    idx = select_menu(valid_types, "  Content type:")
                    if idx is None:
                        print_cancelled()
                        return
                    content_type = valid_types[idx]
            elif content_type not in valid_types:
                print_error(f"Invalid content type '{content_type}'. Valid: {', '.join(valid_types)}")
                raise typer.Exit(1)

            if checksum and not checksum_algorithm:
                # PVE requires checksum-algorithm with checksum: derive it
                # from the length of the hex digest, refuse when it cannot be
                checksum_algorithm = {
                    32: "md5", 40: "sha1", 56: "sha224", 64: "sha256", 96: "sha384", 128: "sha512",
                }.get(len(checksum.strip())) if re.fullmatch(r"[0-9a-fA-F]+", checksum.strip()) else None
                if not checksum_algorithm:
                    print_error(
                        "Cannot tell the algorithm of this checksum: pass --checksum-algorithm "
                        "(md5, sha1, sha224, sha256, sha384, sha512)"
                    )
                    raise typer.Exit(1)

            console.print("\n[bold]Download details:[/bold]")
            console.print(f"  URL:      {url}")
            console.print(f"  Filename: {filename}")
            console.print(f"  Storage:  {storage}")
            console.print(f"  Node:     {node}")
            console.print(f"  Type:     {content_type}")
            if checksum:
                console.print(f"  Checksum: {checksum} ({checksum_algorithm})")

            if not yes and not confirm("Start download?", default=True):
                print_cancelled()
                return

            upid = await client.download_url_to_storage(
                node=node,
                storage=storage,
                url=url,
                filename=filename,
                content_type=content_type,
                checksum=checksum,
                checksum_algorithm=checksum_algorithm,
            )

            if background:
                print_success(f"Download started in background (task: {upid})")
                print_info("Check progress with: pvecli cluster tasks --running")
                return

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task(description=f"Downloading {filename}...", total=None)
                await client.wait_for_task(node, upid, timeout=600)

            print_success(f"'{filename}' downloaded to '{storage}'")

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print()
        if upid:
            print_warning(f"Download continues on Proxmox (task: {upid})")
            print_info("Check progress with: pvecli cluster tasks --running")
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@content_app.command("remove")
@async_to_sync
async def remove_content(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    volume: str = typer.Argument(None, help="Volume ID to delete"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete content from storage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            if not volume:
                content = await client.get_storage_content(node, storage)
                if not content:
                    print_info(f"No content in storage '{storage}'")
                    return

                labels = []
                for item in content:
                    volid = item.get("volid", "?")
                    size = format_bytes(item.get("size", 0)) if item.get("size") else "?"
                    ctype = item.get("content", "?")
                    labels.append(f"{volid} ({ctype}, {size})")

                sel = multi_select_menu(labels, f"  Volumes to delete on '{storage}':")
                if sel is None:
                    print_cancelled()
                    return
                if not sel:
                    print_cancelled()
                    return
                selected_volumes = [content[i].get("volid") for i in sel]
            else:
                selected_volumes = [volume]

            console.print("\n[bold red]Delete volume(s):[/bold red]")
            for vol in selected_volumes:
                console.print(f"  {vol}")
            console.print(f"  Storage: {storage}")
            console.print(f"  Node:    {node}")

            if not yes and not confirm("Delete these volumes?"):
                print_cancelled()
                return

            for vol in selected_volumes:
                await client.delete(f"/nodes/{node}/storage/{storage}/content/{vol}")

            if len(selected_volumes) == 1:
                print_success(f"Volume '{selected_volumes[0]}' deleted")
            else:
                print_success(f"{len(selected_volumes)} volumes deleted")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
