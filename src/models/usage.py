"""Usage aggregation models.

This module is pure: it turns raw /cluster/resources payloads and storage
content listings into aggregated usage models. No I/O, no rendering, so the
whole aggregation can be tested offline.

Traps this module exists to neutralise:
  1. maxdisk on a guest is wrong (it ignores secondary disks) and disk is
     always 0 for QEMU guests, so provisioned space comes from the storage
     content listings, never from the guest entry.
  2. A shared storage is returned once per node and its capacity must be
     counted once, while a non-shared storage returned on several nodes is
     that many distinct capacities and must be summed.
  3. PBS datastores report maxdisk 0 and only hold backups, they never count
     toward production capacity.
  4. Two RADOS pools backed by the same OSDs each report "used + shared free
     space", so a group of them counts as one capacity: the used bytes of
     every pool plus the free space they share. Sharing a Ceph cluster is not
     enough, since two pools on disjoint device classes hold two real
     capacities that add up, hence the conjunction in _group_ceph_storages.
  5. diskread/diskwrite/netin/netout are cumulative counters, not rates, so
     they are deliberately not exposed by any model here.
"""

import re
from typing import Any

from pydantic import BaseModel

GUEST_TYPES = ("qemu", "lxc")
GUEST_DISK_CONTENT = ("images", "rootdir")
BACKUP_PLUGINTYPES = ("pbs",)
CEPH_PLUGINTYPES = ("rbd", "cephfs")
NO_POOL = "(no pool)"

# Trap 4 fallback only, see _same_ceph_free_space. Two RADOS pools of one Ceph
# cluster publish the same free space read twice, so it never matches to the
# byte. Measured on a production cluster: 1 MiB apart out of 12.5 TB, 8e-8 in
# relative terms. This tolerance is five orders of magnitude wider than that
# skew. It has no absolute floor: a floor merges any two nearly full Ceph
# clusters, which silently deletes real capacity.
CEPH_FREE_TOLERANCE = 0.01

# Separators accepted in a "monhost" field: PVE stores it as a free-form list.
CEPH_MONHOST_SEPARATORS = re.compile(r"[\s,;]+")


def _pct(part: float, whole: float) -> float:
    """Return part/whole as a 0..100 percentage, 0.0 when whole is falsy."""
    return (part / whole * 100) if whole else 0.0


def _widest_first(entry: dict[str, Any]) -> tuple[int, str]:
    """Sort key putting the largest maxdisk first, node name breaking ties.

    Ties are broken by node name so the report never depends on the order in
    which /cluster/resources happens to return the nodes.
    """
    return (-int(entry.get("maxdisk") or 0), entry.get("node") or "")


def _same_ceph_free_space(a: int, b: int) -> bool:
    """Tell whether two Ceph storages publish the same cluster free space.

    Heuristic, used only when the storage configuration is unavailable: no
    figure alone can tell two distinct Ceph clusters filled alike apart. The
    monitor set does, see _ceph_monitor_key. Because nothing else backs this
    comparison, two free spaces of zero prove nothing and are refused: two
    unrelated full storages would otherwise be folded into one.

    Args:
        a: Free bytes of the first storage.
        b: Free bytes of the second storage.

    Returns:
        True when both values look like the same figure sampled twice, within
        CEPH_FREE_TOLERANCE of the larger one.
    """
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(a, b) * CEPH_FREE_TOLERANCE


def _same_crush_free_space(a: int, b: int) -> bool:
    """Tell whether two pools of one known Ceph cluster share a CRUSH rule.

    RBDPlugin::status() publishes free = stats.max_avail, and max_avail is
    computed from the CRUSH rule of the pool, hence from its device class. Two
    pools mapped on disjoint classes rest on disjoint OSDs and publish two
    different figures: their capacities really do add up. Two pools of one
    class publish one figure read twice, and count once.

    Unlike _same_ceph_free_space this accepts a pair of zeros: the caller has
    already identified one cluster by its monitors, so two of its pools with
    nothing left are one full capacity, not two.

    Args:
        a: Free bytes published by the first pool.
        b: Free bytes published by the second pool.

    Returns:
        True when both figures are within CEPH_FREE_TOLERANCE of the larger
        one, two zeros counting as the same figure.
    """
    return abs(a - b) <= max(a, b, 1) * CEPH_FREE_TOLERANCE


def _ceph_monitor_key(monhost: str | None) -> tuple[str, ...]:
    """Normalise a "monhost" field into a comparable set of monitor addresses.

    The field is a free-form list of Ceph monitors. Measured on a production
    cluster it is three bare IPv6 addresses separated by spaces, and PVE also
    accepts commas or semicolons, IPv4 or host names, and an optional port
    written "host:6789" or "[2001:db8::1]:6789" for an IPv6 address.

    Args:
        monhost: Raw "monhost" value, or None when the storage uses the
            monitors of the local hyperconverged cluster, which PVE leaves
            implicit by not writing the field at all.

    Returns:
        Monitor addresses, port stripped, lowercased, deduplicated and sorted.
        The empty tuple stands for the local Ceph cluster.
    """
    hosts: set[str] = set()
    for token in CEPH_MONHOST_SEPARATORS.split(monhost or ""):
        if not token:
            continue
        if token.startswith("["):
            # Brackets exist to separate the port from an IPv6 address that is
            # itself full of colons: "[2001:db8::1]:6789".
            host = token[1:].split("]", 1)[0]
        elif token.count(":") == 1:
            # A single colon is never IPv6, it separates a port.
            host = token.split(":", 1)[0]
        else:
            host = token
        if host:
            hosts.add(host.lower())
    return tuple(sorted(hosts))


def _ceph_monitors_by_storage(
    storage_configs: list[dict[str, Any]] | None,
) -> dict[str, tuple[str, ...]]:
    """Map each Ceph storage id to the monitor set it is backed by.

    Args:
        storage_configs: Entries of GET /storage, or None when that call was
            not made or failed. Non Ceph entries are ignored.

    Returns:
        Monitor set per storage id, empty when no configuration is available.
        A storage absent from the result has an unknown Ceph cluster.
    """
    monitors: dict[str, tuple[str, ...]] = {}
    for entry in storage_configs or []:
        name = entry.get("storage")
        # GET /storage names the backend "type" where /cluster/resources
        # names it "plugintype", same values.
        if name and entry.get("type") in CEPH_PLUGINTYPES:
            monitors[name] = _ceph_monitor_key(entry.get("monhost"))
    return monitors


class GuestUsage(BaseModel):
    """Usage of a single guest, derived from one /cluster/resources entry."""

    vmid: int
    name: str
    node: str
    type: str
    status: str
    pool: str | None = None
    template: bool = False
    cpu_allocated: int = 0
    cpu_used: float = 0.0
    cpu_percent: float = 0.0
    mem_allocated: int = 0
    mem_used: int = 0
    mem_percent: float = 0.0
    disk_provisioned: int = 0
    disk_reported: int = 0


class PoolUsage(BaseModel):
    """Usage aggregated over the guests of one pool."""

    poolid: str
    comment: str | None = None
    guests: int = 0
    running: int = 0
    stopped: int = 0
    templates: int = 0
    cpu_allocated: int = 0
    cpu_used: float = 0.0
    cpu_percent: float = 0.0
    mem_allocated: int = 0
    mem_used: int = 0
    mem_percent: float = 0.0
    disk_provisioned: int = 0
    templates_disk_provisioned: int = 0
    members: list[GuestUsage] = []


class NodeUsage(BaseModel):
    """Real load of one hypervisor node."""

    node: str
    status: str
    cpu_allocated: int = 0
    cpu_used: float = 0.0
    cpu_percent: float = 0.0
    mem_allocated: int = 0
    mem_used: int = 0
    mem_percent: float = 0.0
    disk_total: int = 0
    disk_used: int = 0
    disk_percent: float = 0.0
    uptime: int = 0


class StorageUsage(BaseModel):
    """One storage, merged across the nodes that expose it.

    Attributes:
        total: Capacity of a shared storage, or the sum of the per-node
            capacities of a non-shared one. On the pool representing a group of
            RADOS pools, the capacity of the OSDs that group shares.
        used: Same rule as total.
        nodes: Every node exposing the storage, for display.
        available_nodes: Nodes whose own entry answers "available", the only
            ones worth querying for content.
        reason: Why the storage is not counted, or which other pools a counted
            storage aggregates when it stands for a group of RADOS pools.
    """

    storage: str
    plugintype: str
    status: str
    shared: bool = False
    total: int = 0
    used: int = 0
    free: int = 0
    used_percent: float = 0.0
    nodes: list[str] = []
    available_nodes: list[str] = []
    content: list[str] = []
    guest_disks: bool = False
    counted: bool = False
    reason: str | None = None


class StorageProbe(BaseModel):
    """One storage content listing to perform, with its fallback nodes.

    Attributes:
        storage: Storage id to list.
        nodes: Nodes able to answer, in order. Query them one after the other
            and stop at the first success: for a shared storage they all return
            the same content, so the extra nodes are pure fallbacks.
    """

    storage: str
    nodes: list[str] = []


class NodeTotals(BaseModel):
    """Sum of the hypervisor nodes: the real load of the cluster."""

    nodes: int = 0
    online: int = 0
    cpu_allocated: int = 0
    cpu_used: float = 0.0
    cpu_percent: float = 0.0
    mem_allocated: int = 0
    mem_used: int = 0
    mem_percent: float = 0.0
    disk_total: int = 0
    disk_used: int = 0
    disk_percent: float = 0.0


class GuestTotals(BaseModel):
    """Sum of the guests: what the workload itself accounts for."""

    guests: int = 0
    running: int = 0
    stopped: int = 0
    templates: int = 0
    cpu_allocated: int = 0
    cpu_used: float = 0.0
    cpu_percent: float = 0.0
    mem_allocated: int = 0
    mem_used: int = 0
    mem_percent: float = 0.0
    disk_provisioned: int = 0
    templates_disk_provisioned: int = 0


class StorageTotals(BaseModel):
    """Sum of the storages that count toward guest disk capacity."""

    storages: int = 0
    total: int = 0
    used: int = 0
    free: int = 0
    used_percent: float = 0.0


class OverheadUsage(BaseModel):
    """Node load that no guest accounts for: Ceph, PVE, page cache."""

    cpu_used: float = 0.0
    cpu_percent: float = 0.0
    mem_used: int = 0
    mem_percent: float = 0.0


class PoolShare(BaseModel):
    """Share of one pool in its cluster, on explicit denominators.

    Every name reads numerator_percent_of_denominator, and three denominators
    are published because they answer three different questions:
      "_of_physical": the physical capacity of the nodes (installed cores,
        installed memory), what the cluster can hold.
      "_of_nodes": what the hypervisors really consume right now.
      "_of_guests": what all guests together account for.
    A denominator never depends on the numerator: an allocated figure divided
    by the installed capacity is "_of_physical", never "_of_nodes".
    A pool can be 9% of the physical memory, 20% of the memory in use and 40%
    of the guest workload: publishing a single ratio hides that.

    Attributes:
        cpu_used_percent_of_physical: pool cores used / cores installed.
        mem_used_percent_of_physical: pool memory used / memory installed.
        cpu_allocated_percent_of_physical: pool vCPU / cores installed.
        mem_allocated_percent_of_physical: pool memory allocated / installed.
        cpu_used_percent_of_nodes: pool cores used / cores used by the nodes.
        mem_used_percent_of_nodes: pool memory used / memory used by the nodes.
        cpu_used_percent_of_guests: pool cores used / cores used by all guests.
        cpu_allocated_percent_of_guests: pool vCPU / vCPU of all guests.
        mem_used_percent_of_guests: pool memory used / memory used by guests.
        mem_allocated_percent_of_guests: pool allocated / guests allocated.
        disk_percent_of_guests: pool provisioned / provisioned by all guests.
        disk_percent_of_capacity: pool provisioned / guest storage capacity.
    """

    cpu_used_percent_of_physical: float = 0.0
    mem_used_percent_of_physical: float = 0.0
    cpu_allocated_percent_of_physical: float = 0.0
    mem_allocated_percent_of_physical: float = 0.0
    cpu_used_percent_of_nodes: float = 0.0
    mem_used_percent_of_nodes: float = 0.0
    cpu_used_percent_of_guests: float = 0.0
    cpu_allocated_percent_of_guests: float = 0.0
    mem_used_percent_of_guests: float = 0.0
    mem_allocated_percent_of_guests: float = 0.0
    disk_percent_of_guests: float = 0.0
    disk_percent_of_capacity: float = 0.0


class ClusterUsage(BaseModel):
    """Full cluster usage report."""

    nodes: list[NodeUsage] = []
    node_totals: NodeTotals
    guest_totals: GuestTotals
    overhead: OverheadUsage
    pools: list[PoolUsage] = []
    storages: list[StorageUsage] = []
    storage_totals: StorageTotals
    reference_storage: str | None = None
    cpu_overcommit: float = 0.0
    mem_overcommit: float = 0.0
    disk_exact: bool = False


def build_guest_usage(
    resources: list[dict[str, Any]],
    volumes_by_vmid: dict[int, int] | None = None,
) -> list[GuestUsage]:
    """Build per-guest usage from a /cluster/resources payload.

    Args:
        resources: Raw /cluster/resources entries (any type, filtered here).
        volumes_by_vmid: Provisioned bytes per vmid, from sum_volumes_by_vmid.
            When omitted, disk_provisioned stays 0 for every guest.

    Returns:
        Guests sorted by vmid, templates included and flagged.
    """
    volumes = volumes_by_vmid or {}
    guests: list[GuestUsage] = []
    for r in resources:
        if r.get("type") not in GUEST_TYPES:
            continue
        vmid = int(r.get("vmid") or 0)
        maxcpu = int(r.get("maxcpu") or 0)
        # "cpu" is a 0..1 fraction of this guest's own maxcpu, never a percentage
        cpu_used = float(r.get("cpu") or 0.0) * maxcpu
        mem_used = int(r.get("mem") or 0)
        mem_allocated = int(r.get("maxmem") or 0)
        guests.append(
            GuestUsage(
                vmid=vmid,
                name=r.get("name") or str(vmid),
                node=r.get("node") or "",
                type=r.get("type") or "",
                status=r.get("status") or "unknown",
                pool=r.get("pool") or None,
                template=bool(r.get("template")),
                cpu_allocated=maxcpu,
                cpu_used=cpu_used,
                cpu_percent=_pct(cpu_used, maxcpu),
                mem_allocated=mem_allocated,
                mem_used=mem_used,
                mem_percent=_pct(mem_used, mem_allocated),
                # Trap 1: maxdisk on a guest ignores secondary disks and disk is
                # always 0 for QEMU, so the real figure only comes from volumes.
                disk_provisioned=volumes.get(vmid, 0),
                disk_reported=int(r.get("maxdisk") or 0),
            )
        )
    return sorted(guests, key=lambda g: g.vmid)


def build_node_usage(resources: list[dict[str, Any]]) -> list[NodeUsage]:
    """Build per-node usage from a /cluster/resources payload.

    Args:
        resources: Raw /cluster/resources entries.

    Returns:
        Nodes sorted by name.
    """
    nodes: list[NodeUsage] = []
    for r in resources:
        if r.get("type") != "node":
            continue
        maxcpu = int(r.get("maxcpu") or 0)
        cpu_used = float(r.get("cpu") or 0.0) * maxcpu
        mem_used = int(r.get("mem") or 0)
        mem_total = int(r.get("maxmem") or 0)
        # disk_total/disk_used are the node local rootfs, never a cluster capacity
        disk_used = int(r.get("disk") or 0)
        disk_total = int(r.get("maxdisk") or 0)
        nodes.append(
            NodeUsage(
                node=r.get("node") or "",
                status=r.get("status") or "unknown",
                cpu_allocated=maxcpu,
                cpu_used=cpu_used,
                cpu_percent=_pct(cpu_used, maxcpu),
                mem_allocated=mem_total,
                mem_used=mem_used,
                mem_percent=_pct(mem_used, mem_total),
                disk_total=disk_total,
                disk_used=disk_used,
                disk_percent=_pct(disk_used, disk_total),
                uptime=int(r.get("uptime") or 0),
            )
        )
    return sorted(nodes, key=lambda n: n.node)


def _group_ceph_storages(
    candidates: list[StorageUsage],
    monitors: dict[str, tuple[str, ...]],
) -> list[list[StorageUsage]]:
    """Group the Ceph storages that share one capacity.

    Two conditions together, and neither of them alone:
        Same monitor set, read from the storage configuration and not guessed
            from a numeric coincidence: two clusters filled alike are two
            capacities however close their figures look.
        Same published free space: inside one cluster, free is the max_avail of
            the CRUSH rule of the pool, so two pools on disjoint device classes
            (nvme and hdd) rest on disjoint OSDs and hold two real capacities
            that add up. Grouping on the monitors alone would throw the free
            space of one class away.

    A storage missing from the configuration has no monitor set at all. It
    keeps the free space heuristic and is only ever compared to other storages
    in the same situation, never folded into an identified cluster: a matching
    free space is not evidence of a shared cluster. Two pools of one cluster
    can therefore stay apart when only one of them is configured, which
    over-counts that cluster once. That is the safe direction, the opposite
    choice deletes real capacity silently, and it takes an answer to
    /cluster/resources that GET /storage does not cover, while both endpoints
    filter on the same Datastore.Audit permission. The same over-count happens
    when only one pool of a cluster carries monhost, and when two pools spell
    the same monitors as host names on one side and as addresses on the other,
    both for the same reason and in the same safe direction.

    Args:
        candidates: Ceph storages to group, best representative first.
        monitors: Monitor set per storage id, from _ceph_monitors_by_storage. A
            storage absent from this mapping has an unknown Ceph cluster.

    Returns:
        One list per shared capacity, each keeping the order of candidates. One
        cluster spread over several device classes yields several lists.
    """
    groups: list[list[StorageUsage]] = []
    keys: list[tuple[str, ...] | None] = []
    for s in candidates:
        key = monitors.get(s.storage)
        if key is not None:
            # Both signals at once, compared against the head of the group so a
            # group of three pools never chains three tolerances end to end.
            index = next(
                (
                    i
                    for i, k in enumerate(keys)
                    if k == key and _same_crush_free_space(groups[i][0].free, s.free)
                ),
                None,
            )
        else:
            index = next(
                (
                    i
                    for i, group in enumerate(groups)
                    if keys[i] is None and _same_ceph_free_space(group[0].free, s.free)
                ),
                None,
            )
        if index is None:
            groups.append([s])
            keys.append(key)
        else:
            groups[index].append(s)
    return groups


def build_storage_usage(
    resources: list[dict[str, Any]],
    storage_configs: list[dict[str, Any]] | None = None,
) -> list[StorageUsage]:
    """Build deduplicated storage usage from a /cluster/resources payload.

    Traps handled here:
        Trap 2: a shared storage appears once per node with the same capacity
            every time, so it is counted once. A non-shared storage appears
            once per node with a capacity of its own, so those are summed.
        Trap 3: PBS datastores report maxdisk 0 and only hold backups, they are
            flagged counted=False.
        Trap 4: RADOS pools sharing the same OSDs each report their own used
            bytes plus the free space left on those OSDs, so a group of them
            is folded into a single exact capacity. Pools of one cluster on
            distinct device classes are not such a group, see
            _group_ceph_storages.

    Args:
        resources: Raw /cluster/resources entries.
        storage_configs: Entries of GET /storage, which carry the "monhost"
            field telling which Ceph cluster a storage is a pool of. Omit or
            pass None when that call failed: trap 4 then falls back to the
            free space heuristic, which is weaker.

    Returns:
        One StorageUsage per storage id, sorted by name.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in resources:
        if r.get("type") != "storage":
            continue
        name = r.get("storage")
        if not name:
            continue
        grouped.setdefault(name, []).append(r)

    storages: list[StorageUsage] = []
    for name, entries in grouped.items():
        # A single node may disagree: an offline one reports maxdisk 0, so the
        # widest entry is the one that describes the storage best.
        widest = sorted(entries, key=_widest_first)[0]
        shared = any(e.get("shared") for e in entries)
        if shared:
            # Trap 2: every node reports the same capacity, count it once.
            total = int(widest.get("maxdisk") or 0)
            used = int(widest.get("disk") or 0)
        else:
            # /cluster/resources returns one entry per (node, storage) pair, and
            # each one is a distinct local capacity: sum them.
            total = sum(int(e.get("maxdisk") or 0) for e in entries)
            used = sum(int(e.get("disk") or 0) for e in entries)

        nodes = sorted({e.get("node") or "" for e in entries} - {""})
        available_nodes = sorted(
            {e.get("node") or "" for e in entries if e.get("status") == "available"} - {""}
        )
        plugintype = widest.get("plugintype") or ""
        status = "available" if available_nodes else (widest.get("status") or "unknown")
        content = [c for c in (widest.get("content") or "").split(",") if c]
        guest_disks = any(c in GUEST_DISK_CONTENT for c in content)

        if plugintype in BACKUP_PLUGINTYPES:
            counted, reason = False, "backup datastore"
        elif status != "available" or total <= 0:
            counted, reason = False, "unavailable"
        elif not guest_disks:
            counted, reason = False, "no guest disk content"
        else:
            counted, reason = True, None

        storages.append(
            StorageUsage(
                storage=name,
                plugintype=plugintype,
                status=status,
                shared=shared,
                total=total,
                used=used,
                free=max(0, total - used),
                used_percent=_pct(used, total),
                nodes=nodes,
                available_nodes=available_nodes,
                content=content,
                guest_disks=guest_disks,
                counted=counted,
                reason=reason,
            )
        )

    # Trap 4: several RADOS pools sharing one set of OSDs each report "what
    # this pool uses + the free space left on those OSDs". Summing them
    # multiplies that capacity, and keeping only one of them loses the used
    # bytes of the others. The exact capacity of a group is the sum of the used
    # bytes of its pools plus the free space they all publish, so the group is
    # folded onto its largest pool and the others are left visible but not
    # counted. Pools of one cluster on disjoint device classes do not share
    # their OSDs and are not one group, see _group_ceph_storages.
    candidates = sorted(
        [x for x in storages if x.counted and x.shared and x.plugintype in CEPH_PLUGINTYPES],
        key=lambda x: (-x.total, x.storage),
    )
    for group in _group_ceph_storages(candidates, _ceph_monitors_by_storage(storage_configs)):
        head, others = group[0], group[1:]
        if not others:
            continue
        # head.free is the space left on the OSDs the group shares, published
        # the same way by every pool of the group, so the exact capacity is
        # that free space plus the used bytes of all the pools.
        head.used = sum(s.used for s in group)
        head.total = head.used + head.free
        head.used_percent = _pct(head.used, head.total)
        head.reason = "Ceph capacity shared with " + ", ".join(f"'{s.storage}'" for s in others)
        for s in others:
            s.counted = False
            s.reason = f"same Ceph disks as '{head.storage}', counted there"

    return sorted(storages, key=lambda s: s.storage)


def select_reference_storage(
    storages: list[StorageUsage],
    preferred: str | None = None,
) -> StorageUsage | None:
    """Pick the storage used as the capacity reference for guest disks.

    Deterministic rule, applied to the storages that count toward capacity:
    shared first, then the largest total, then the first name in alphabetical
    order. Shared comes first because guest disks in a cluster live on shared
    storage: a bigger local scratch disk would be a misleading denominator.

    Args:
        storages: Storages built by build_storage_usage.
        preferred: Exact storage id forced by --storage. Returns None when that
            storage does not exist.

    Returns:
        The reference storage, or None when there is no candidate.
    """
    if preferred:
        return next((s for s in storages if s.storage == preferred), None)
    candidates = [s for s in storages if s.counted]
    if not candidates:
        return None
    return sorted(candidates, key=lambda s: (not s.shared, -s.total, s.storage))[0]


def storage_probe_plan(
    storages: list[StorageUsage],
    preferred: str | None = None,
) -> list[StorageProbe]:
    """Return the storage content listings to perform, fallbacks included.

    A shared storage yields a single listing, its content is identical
    everywhere (trap 2 again, applied to the content listing), the other nodes
    are kept as fallbacks. A local storage yields one listing per node, each
    holding volumes of its own.

    Only nodes whose own entry reports "available" are listed: querying a node
    that is rebooting fails the whole scan and drops every provisioned disk of
    the cluster, when another node of the same shared storage would have
    answered.

    Selection uses guest_disks, not counted: a Ceph pool excluded from the
    capacity total still holds real volumes whose size must be summed.

    Args:
        storages: Storages built by build_storage_usage.
        preferred: Restrict the scan to this storage id.

    Returns:
        Listings sorted by (storage, node), deterministic.
    """
    plan: list[StorageProbe] = []
    for s in sorted(storages, key=lambda x: x.storage):
        if not s.guest_disks or not s.available_nodes:
            continue
        if preferred and s.storage != preferred:
            continue
        if s.shared:
            plan.append(StorageProbe(storage=s.storage, nodes=list(s.available_nodes)))
        else:
            plan.extend(StorageProbe(storage=s.storage, nodes=[n]) for n in s.available_nodes)
    return plan


def storage_probe_targets(
    storages: list[StorageUsage],
    preferred: str | None = None,
) -> list[tuple[str, str]]:
    """Return the (node, storage) pairs to query for guest volumes.

    Flat view of storage_probe_plan: the first node of each listing, without
    its fallbacks. Callers able to retry should use storage_probe_plan.

    Args:
        storages: Storages built by build_storage_usage.
        preferred: Restrict the scan to this storage id.

    Returns:
        Pairs sorted by (storage, node), deterministic.
    """
    return [(p.nodes[0], p.storage) for p in storage_probe_plan(storages, preferred)]


def sum_volumes_by_vmid(volumes: list[dict[str, Any]]) -> dict[int, int]:
    """Sum provisioned bytes per vmid from storage content listings.

    Only guest disk volumes are summed: backups, ISO images and container
    templates either carry no vmid or a content type that is not a disk.
    Volumes are deduplicated by volid, so a storage listed twice cannot inflate
    the total.

    Args:
        volumes: Raw items from /nodes/{node}/storage/{storage}/content.

    Returns:
        Mapping vmid to total provisioned bytes.
    """
    totals: dict[int, int] = {}
    seen: set[str] = set()
    for v in volumes:
        if v.get("content") not in GUEST_DISK_CONTENT:
            continue
        vmid = v.get("vmid")
        volid = v.get("volid") or ""
        if vmid is None or volid in seen:
            continue
        seen.add(volid)
        totals[int(vmid)] = totals.get(int(vmid), 0) + int(v.get("size") or 0)
    return totals


def aggregate_pools(
    guests: list[GuestUsage],
    with_members: bool = False,
) -> list[PoolUsage]:
    """Group guests by pool.

    The "pool" field is filled by PVE on every guest that belongs to a pool, so
    this is a plain group-by on a single API call: /pools is never needed here.
    Templates are excluded from every total and tracked in their own fields.

    Args:
        guests: Guests built by build_guest_usage.
        with_members: Attach the guest list to each pool (pool scope only, it
            would bloat a cluster wide report).

    Returns:
        Pools sorted by id, the "(no pool)" bucket last.
    """
    buckets: dict[str, list[GuestUsage]] = {}
    for g in guests:
        buckets.setdefault(g.pool or NO_POOL, []).append(g)

    pools: list[PoolUsage] = []
    for poolid, members in buckets.items():
        real = [g for g in members if not g.template]
        templates = [g for g in members if g.template]
        cpu_allocated = sum(g.cpu_allocated for g in real)
        cpu_used = sum(g.cpu_used for g in real)
        mem_allocated = sum(g.mem_allocated for g in real)
        mem_used = sum(g.mem_used for g in real)
        pools.append(
            PoolUsage(
                poolid=poolid,
                guests=len(real),
                running=sum(1 for g in real if g.status == "running"),
                stopped=sum(1 for g in real if g.status != "running"),
                templates=len(templates),
                cpu_allocated=cpu_allocated,
                cpu_used=cpu_used,
                cpu_percent=_pct(cpu_used, cpu_allocated),
                mem_allocated=mem_allocated,
                mem_used=mem_used,
                mem_percent=_pct(mem_used, mem_allocated),
                disk_provisioned=sum(g.disk_provisioned for g in real),
                templates_disk_provisioned=sum(g.disk_provisioned for g in templates),
                members=sorted(members, key=lambda g: g.vmid) if with_members else [],
            )
        )
    return sorted(pools, key=lambda p: (p.poolid == NO_POOL, p.poolid))


def totalize_nodes(nodes: list[NodeUsage]) -> NodeTotals:
    """Sum the node usages into the real cluster load."""
    cpu_allocated = sum(n.cpu_allocated for n in nodes)
    cpu_used = sum(n.cpu_used for n in nodes)
    mem_allocated = sum(n.mem_allocated for n in nodes)
    mem_used = sum(n.mem_used for n in nodes)
    disk_total = sum(n.disk_total for n in nodes)
    disk_used = sum(n.disk_used for n in nodes)
    return NodeTotals(
        nodes=len(nodes),
        online=sum(1 for n in nodes if n.status == "online"),
        cpu_allocated=cpu_allocated,
        cpu_used=cpu_used,
        cpu_percent=_pct(cpu_used, cpu_allocated),
        mem_allocated=mem_allocated,
        mem_used=mem_used,
        mem_percent=_pct(mem_used, mem_allocated),
        disk_total=disk_total,
        disk_used=disk_used,
        disk_percent=_pct(disk_used, disk_total),
    )


def totalize_guests(guests: list[GuestUsage]) -> GuestTotals:
    """Sum the guest usages, templates excluded from every total."""
    real = [g for g in guests if not g.template]
    templates = [g for g in guests if g.template]
    cpu_allocated = sum(g.cpu_allocated for g in real)
    cpu_used = sum(g.cpu_used for g in real)
    mem_allocated = sum(g.mem_allocated for g in real)
    mem_used = sum(g.mem_used for g in real)
    return GuestTotals(
        guests=len(real),
        running=sum(1 for g in real if g.status == "running"),
        stopped=sum(1 for g in real if g.status != "running"),
        templates=len(templates),
        cpu_allocated=cpu_allocated,
        cpu_used=cpu_used,
        cpu_percent=_pct(cpu_used, cpu_allocated),
        mem_allocated=mem_allocated,
        mem_used=mem_used,
        mem_percent=_pct(mem_used, mem_allocated),
        disk_provisioned=sum(g.disk_provisioned for g in real),
        templates_disk_provisioned=sum(g.disk_provisioned for g in templates),
    )


def totalize_storages(storages: list[StorageUsage]) -> StorageTotals:
    """Sum only the storages that count toward guest disk capacity."""
    counted = [s for s in storages if s.counted]
    total = sum(s.total for s in counted)
    used = sum(s.used for s in counted)
    return StorageTotals(
        storages=len(counted),
        total=total,
        used=used,
        free=max(0, total - used),
        used_percent=_pct(used, total),
    )


def compute_cluster_usage(
    resources: list[dict[str, Any]],
    volumes_by_vmid: dict[int, int] | None = None,
    storage_filter: str | None = None,
    disk_exact: bool = False,
    storages: list[StorageUsage] | None = None,
) -> ClusterUsage:
    """Compute the full cluster usage report.

    Node totals are the real hypervisor load, guest totals are the sum of the
    workload. The difference is the overhead: Ceph OSDs, PVE services, page
    cache. Both are reported so the gap is visible instead of implied.

    Args:
        resources: Raw entries from an unfiltered /cluster/resources call.
        volumes_by_vmid: Provisioned bytes per vmid, or None to skip disks.
        storage_filter: Storage id forced by --storage, used for the reference.
        disk_exact: True when the scan covered every storage holding guest
            disks and all of its listings succeeded. False when --storage
            narrowed it: the figures are then real but partial.
        storages: Storages already built by build_storage_usage, reused as is by
            a caller that needed them to plan its content listings. Built from
            resources when omitted.

    Returns:
        The cluster usage report.
    """
    guests = build_guest_usage(resources, volumes_by_vmid)
    nodes = build_node_usage(resources)
    storage_usages = build_storage_usage(resources) if storages is None else storages
    node_totals = totalize_nodes(nodes)
    guest_totals = totalize_guests(guests)
    reference = select_reference_storage(storage_usages, storage_filter)

    # No clamp: a negative overhead means the node and guest samples are not
    # synchronous, which is a real signal and must not be hidden.
    cpu_overhead = node_totals.cpu_used - guest_totals.cpu_used
    mem_overhead = node_totals.mem_used - guest_totals.mem_used

    return ClusterUsage(
        nodes=nodes,
        node_totals=node_totals,
        guest_totals=guest_totals,
        overhead=OverheadUsage(
            cpu_used=cpu_overhead,
            cpu_percent=_pct(cpu_overhead, node_totals.cpu_used),
            mem_used=mem_overhead,
            mem_percent=_pct(mem_overhead, node_totals.mem_used),
        ),
        pools=aggregate_pools(guests),
        storages=storage_usages,
        storage_totals=totalize_storages(storage_usages),
        reference_storage=reference.storage if reference else None,
        cpu_overcommit=(
            guest_totals.cpu_allocated / node_totals.cpu_allocated
            if node_totals.cpu_allocated
            else 0.0
        ),
        mem_overcommit=(
            guest_totals.mem_allocated / node_totals.mem_allocated
            if node_totals.mem_allocated
            else 0.0
        ),
        disk_exact=disk_exact,
    )


def compute_pool_share(pool: PoolUsage, cluster: ClusterUsage) -> PoolShare:
    """Compute the share of one pool against every meaningful denominator.

    Args:
        pool: Usage of the pool.
        cluster: Usage of the whole cluster, computed from the same payload.

    Returns:
        Percentages on a 0..100 scale, see PoolShare for what each one divides.
    """
    nt = cluster.node_totals
    gt = cluster.guest_totals
    st = cluster.storage_totals
    return PoolShare(
        cpu_used_percent_of_physical=_pct(pool.cpu_used, nt.cpu_allocated),
        mem_used_percent_of_physical=_pct(pool.mem_used, nt.mem_allocated),
        cpu_allocated_percent_of_physical=_pct(pool.cpu_allocated, nt.cpu_allocated),
        mem_allocated_percent_of_physical=_pct(pool.mem_allocated, nt.mem_allocated),
        cpu_used_percent_of_nodes=_pct(pool.cpu_used, nt.cpu_used),
        mem_used_percent_of_nodes=_pct(pool.mem_used, nt.mem_used),
        cpu_used_percent_of_guests=_pct(pool.cpu_used, gt.cpu_used),
        cpu_allocated_percent_of_guests=_pct(pool.cpu_allocated, gt.cpu_allocated),
        mem_used_percent_of_guests=_pct(pool.mem_used, gt.mem_used),
        mem_allocated_percent_of_guests=_pct(pool.mem_allocated, gt.mem_allocated),
        disk_percent_of_guests=_pct(pool.disk_provisioned, gt.disk_provisioned),
        disk_percent_of_capacity=_pct(pool.disk_provisioned, st.total),
    )


def compute_pool_usage(
    resources: list[dict[str, Any]],
    poolid: str,
    volumes_by_vmid: dict[int, int] | None = None,
    comment: str | None = None,
) -> PoolUsage:
    """Compute the usage of a single pool, members included.

    Args:
        resources: Raw entries from an unfiltered /cluster/resources call.
        poolid: Pool to report on.
        volumes_by_vmid: Provisioned bytes per vmid, or None to skip disks.
        comment: Pool comment, taken from GET /pools/{poolid}.

    Returns:
        The pool usage. An existing pool with no member yields a zeroed model,
        existence itself is checked by the caller through client.get_pool.
    """
    guests = build_guest_usage(resources, volumes_by_vmid)
    pools = aggregate_pools(guests, with_members=True)
    usage = next((p for p in pools if p.poolid == poolid), PoolUsage(poolid=poolid))
    usage.comment = comment
    return usage
