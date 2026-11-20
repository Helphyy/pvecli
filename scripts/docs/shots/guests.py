"""Captures of the Guests section (docs/guests/).

Test guests live in 800-819 and are removed afterwards. 819 debian-13 is a
cloud-init template (Debian 13 cloud image with qemu-guest-agent, net0 on
vmbr0) made for these captures. Every shot that needs those guests or
changes the lab is manual: run it by name once the guests exist.

The shots that read ~/.ssh/id_ed25519.pub (vm deploy, ct deploy, deploy) run
with HOME pointing to a folder holding that key and a link to the real
~/.config/pvecli; guests-deploy-run also needs DNS_ROOT_PASSWORD set.
docs/guests/guests.yaml holds documentation addresses (10.0.0.x): for the
guests-deploy-* shots, put the lab addresses that screens.local.toml maps
to them in the file, then put the documentation ones back.
"""

from scripts.docs.casts import DOWN, ENTER, SPACE, Cast
from scripts.docs.screens import Shot

YES = [("(n): ", "y")]
NO = [("(n): ", "n")]

SHOTS = [
    # Create a VM
    Shot("guests-vm-template", ["vm", "template", "819", "-p", "{profile}"],
         "A stopped VM converted to a template, after the confirmation",
         stdin="y\n", echo=YES, marks=["converted to template"], manual=True),
    Shot("guests-vm-deploy",
         ["vm", "deploy", "819", "web-02", "--vmid", "801", "--cores", "2", "--memory", "2G",
          "--disk-size", "10G", "--user", "debian", "--ssh-key", "~/.ssh/id_ed25519.pub",
          "--ip", "10.0.0.21/24", "--gw", "10.0.0.1", "--tags", "web", "--wait-ip", "180",
          "-p", "{profile}"],
         "A VM deployed from the template: clone, size, cloud-init, start, IP from the agent",
         marks=["linked", "is up at 10.0.0.21"], manual=True),
    Shot("guests-vm-deploy-check",
         ["vm", "deploy", "819", "web-04", "--vmid", "801", "--storage", "nfs-vm", "--disk-size", "2G",
          "--pool", "dmz", "-p", "{profile}"],
         "Every problem of a deploy reported at once, before anything is created",
         marks=[], manual=True),
    Shot("guests-vm-add",
         ["vm", "add", "pve1", "--vmid", "802", "--name", "app-01", "--iso-storage", "local",
          "--iso", "debian-13.6.0-amd64-netinst.iso", "--os-type", "linux", "--cores", "2",
          "--memory", "2048", "--disk-storage", "local-lvm", "--disk-size", "16", "--bridge", "vmbr0",
          "-p", "{profile}"],
         "A VM created from an ISO with options, no question asked",
         marks=["created successfully"], manual=True),
    Shot("guests-vm-clone", ["vm", "clone", "819", "--newid", "803", "--name", "web-03", "-p", "{profile}"],
         "A linked clone of the template, non-interactive",
         marks=["cloned to 803"], manual=True),
    Shot("guests-vm-cloudinit-set",
         ["vm", "cloudinit", "set", "801", "--search-domain", "example.com", "-p", "{profile}"],
         "Cloud-init changed on a running VM: applied at its next reboot",
         marks=["next reboot"], manual=True),
    Shot("guests-vm-cloudinit-show", ["vm", "cloudinit", "show", "801", "-p", "{profile}"],
         "The cloud-init settings of the deployed VM", manual=True),
    # Create a container
    Shot("guests-ct-image-list", ["ct", "image", "list", "pve1", "local", "-p", "{profile}"],
         "The LXC images already on a storage of a node", manual=True),
    Shot("guests-ct-image-add",
         ["ct", "image", "add", "pve1", "local", "--name", "alpine-3.24-default_20261105_amd64.tar.xz",
          "-p", "{profile}"],
         "An LXC image downloaded from the Proxmox repository by its name",
         marks=["downloaded successfully"], manual=True),
    Shot("guests-ct-deploy",
         ["ct", "deploy", "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst", "--hostname", "dns-01",
          "--ctid", "805", "--node", "pve1", "--storage", "local-lvm", "--disk-size", "4G", "--memory", "512M",
          "--ip", "10.0.0.22/24", "--gw", "10.0.0.1", "--ssh-key", "~/.ssh/id_ed25519.pub",
          "--tags", "dns", "--wait-ip", "60", "-p", "{profile}"],
         "A container created, started and reachable in one command, then the command to do it again",
         manual=True),
    Shot("guests-ct-add",
         ["ct", "add", "pve1", "--ctid", "806", "--hostname", "proxy-01", "--template-storage", "local",
          "--template", "debian-13-standard_13.6-1_amd64.tar.zst", "--rootfs-storage", "local-lvm",
          "--rootfs-size", "4", "--memory", "512", "--bridge", "vmbr0", "--ip", "10.0.0.23/24",
          "--gateway", "10.0.0.1", "-p", "{profile}"],
         "A container created with options, no question asked", manual=True),
    Shot("guests-ct-clone", ["ct", "clone", "806", "--newid", "807", "--hostname", "proxy-02", "-p", "{profile}"],
         "A stopped container cloned (full copy)", manual=True),
    # Deploy guests from a YAML file (docs/guests/guests.yaml)
    Shot("guests-deploy-dry-run", ["deploy", "guests.yaml", "--dry-run", "-p", "{profile}"],
         "The plan of the file, checked on the cluster, nothing created",
         cwd="docs/guests", width=100, marks=["Dry run"], manual=True),
    Shot("guests-deploy-run", ["deploy", "guests.yaml", "--wait-ip", "120", "-p", "{profile}"],
         "The file deployed: plan, confirmation, the guests built side by side, the result",
         stdin="y\n", echo=YES, cwd="docs/guests", width=100, manual=True),
    Shot("guests-deploy-again", ["deploy", "guests.yaml", "-p", "{profile}"],
         "The same file run again: every guest is present, nothing is created",
         cwd="docs/guests", width=100, marks=["Nothing to create"], manual=True),
    # Start, stop and reboot guests
    Shot("guests-vm-start", ["vm", "start", "802,803", "-p", "{profile}"],
         "Two VMs started by ID, one after the other", marks=["Summary"], manual=True),
    Shot("guests-vm-stop", ["vm", "stop", "802", "-p", "{profile}"],
         "A hard stop, confirmed first", stdin="y\n", echo=YES, manual=True),
    Shot("guests-vm-shutdown-tag", ["vm", "shutdown", "--tag", "api", "--timeout", "120", "-p", "{profile}"],
         "The VMs carrying a tag shut down cleanly: the selection is printed, then confirmed",
         stdin="y\n", echo=YES, manual=True),
    Shot("guests-cluster-guests-start", ["cluster", "guests", "start", "--tag", "api", "-p", "{profile}"],
         "Every guest carrying a tag started in parallel", stdin="y\n", echo=YES, manual=True),
    Shot("guests-vm-suspend", ["vm", "suspend", "803", "-y", "-p", "{profile}"],
         "A VM paused in memory", manual=True),
    Shot("guests-vm-resume", ["vm", "resume", "803", "-y", "-p", "{profile}"],
         "The paused VM running again", manual=True),
    Shot("guests-vm-shutdown-timeout", ["vm", "shutdown", "802", "--timeout", "30", "-y", "-p", "{profile}"],
         "A VM that ignores the shutdown request (no OS installed): the timeout runs out",
         marks=["within 30 seconds"], manual=True),
    # Open a console or a shell
    Shot("guests-vm-exec", ["vm", "exec", "810,811", "-p", "{profile}", "--", "hostname", "-I"],
         "A command run in two VMs through the guest agent: one header per VM, then a summary",
         marks=["Summary"], manual=True),
    Shot("guests-vm-ssh-noagent", ["vm", "ssh", "802", "-p", "{profile}"],
         "SSH refused: the VM runs but no guest agent answers (no OS installed yet)",
         marks=["Guest Agent not available"], manual=True),
    Shot("guests-vm-exec-noagent", ["vm", "exec", "802", "-p", "{profile}", "--", "uptime"],
         "vm exec on a VM without a running guest agent", manual=True),
    # Migrate a guest
    Shot("guests-vm-migrate", ["vm", "migrate", "804", "pve2", "-p", "{profile}"],
         "A running VM on Ceph moved live: plan, confirmation, done",
         stdin="y\n", echo=[("(y): ", "y")], marks=["live"], manual=True),
    Shot("guests-vm-migrate-local", ["vm", "migrate", "803", "pve2", "-p", "{profile}"],
         "A running VM with a local disk refused before anything moves", manual=True),
    Shot("guests-vm-migrate-disks", ["vm", "migrate", "803", "pve2", "--with-local-disks", "-p", "{profile}"],
         "The same VM moved live, its local disks copied to the target",
         stdin="y\n", echo=[("(y): ", "y")], marks=["migrated"], manual=True),
    Shot("guests-ct-migrate", ["ct", "migrate", "806", "pve2", "--restart", "-p", "{profile}"],
         "A running container moved in restart mode, its local disk copied",
         stdin="y\n", echo=[("(y): ", "y")], marks=["restart"], manual=True),
    # Take and roll back snapshots
    Shot("guests-vm-snapshot-add",
         ["vm", "snapshot", "add", "810", "before-nginx", "-de", "Before nginx 1.28", "-w", "-p", "{profile}"],
         "A snapshot of one VM, waited for", manual=True),
    Shot("guests-vm-snapshot-list", ["vm", "snapshot", "list", "810", "-p", "{profile}"],
         "The snapshots of one VM", manual=True),
    Shot("guests-snapshot-add",
         ["snapshot", "add", "pre-upgrade", "--vms", "810-812", "-d", "Before the Debian point release",
          "-p", "{profile}"],
         "The same snapshot on two VMs and a container, confirmed first",
         stdin="y\n", echo=YES, marks=["3/3"], manual=True),
    Shot("guests-snapshot-list", ["snapshot", "list", "--vms", "810-812", "-p", "{profile}"],
         "The snapshots of several guests with their age", width=100, manual=True),
    Shot("guests-vm-snapshot-rollback",
         ["vm", "snapshot", "rollback", "810", "before-nginx", "-w", "-p", "{profile}"],
         "A VM rolled back to a snapshot taken without its RAM, waited for",
         stdin="y\n", echo=YES, manual=True),
    # Make a guest highly available
    Shot("guests-vm-ha-add",
         ["vm", "ha", "add", "804", "--nodes", "pve2,pve3", "--comment", "web front", "-y", "-p", "{profile}"],
         "A VM put under HA with preferred nodes (an HA rule on PVE 9), defaults for the rest",
         marks=["registered"], manual=True),
    Shot("guests-ct-ha-add", ["ct", "ha", "add", "808", "-y", "-p", "{profile}"],
         "A container put under HA with the defaults", manual=True),
    Shot("guests-vm-ha-status-one", ["vm", "ha", "status", "804", "-p", "{profile}"],
         "The HA state of one VM", manual=True),
    Shot("guests-vm-ha-status", ["vm", "ha", "status", "-p", "{profile}"],
         "Every HA-managed VM", width=100, manual=True),
    Shot("guests-vm-ha-remove", ["vm", "ha", "remove", "804", "-p", "{profile}"],
         "A VM taken out of HA, its rule with it", stdin="y\n", echo=YES, manual=True),
    # Organize guests with tags and pools (tags.json and pools.json are
    # written to docs/guests/: delete them after the shots)
    Shot("guests-vm-tag-add", ["vm", "tag", "add", "804", "web,debian", "-p", "{profile}"],
         "Two tags added to a VM", manual=True),
    Shot("guests-ct-tag-add", ["ct", "tag", "add", "806", "proxy", "-p", "{profile}"],
         "A tag added to a container", manual=True),
    Shot("guests-tag-color", ["tag", "add", "api", "--color", "1e88e5", "-p", "{profile}"],
         "A color given to a tag, cluster-wide", manual=True),
    Shot("guests-tag-list", ["tag", "list", "-p", "{profile}"],
         "Every tag of the cluster, its color and how many guests carry it", manual=True),
    Shot("guests-tag-edit", ["tag", "edit", "dns", "--name", "resolver", "-p", "{profile}"],
         "A tag renamed on every guest that carries it", manual=True),
    Shot("guests-tag-remove", ["tag", "remove", "resolver", "-p", "{profile}"],
         "A tag taken off every guest, confirmed first", stdin="y\n", echo=YES, manual=True),
    Shot("guests-tag-export", ["tag", "export", "-o", "tags.json", "-p", "{profile}"],
         "The tags and colors of a cluster written to a file", cwd="docs/guests", manual=True),
    Shot("guests-tag-import", ["tag", "import", "tags.json", "-p", "staging"],
         "The colors of that file applied to another cluster", cwd="docs/guests", manual=True),
    Shot("guests-pool-add", ["pool", "add", "frontend", "-c", "Web front ends", "-p", "{profile}"],
         "A resource pool created", manual=True),
    Shot("guests-pool-content-add", ["pool", "content", "add", "frontend", "804,806,810,811", "-p", "{profile}"],
         "Guests put in the pool", manual=True),
    Shot("guests-pool-usage", ["pool", "usage", "frontend", "-p", "{profile}"],
         "What the guests of a pool use, added up", width=100, manual=True),
    Shot("guests-pool-export", ["pool", "export", "-o", "pools.json", "-p", "{profile}"],
         "The pools of a cluster written to a file", cwd="docs/guests", manual=True),
    Shot("guests-pool-import", ["pool", "import", "pools.json", "-p", "staging"],
         "The pools of that file created on another cluster", cwd="docs/guests", manual=True),
    Shot("guests-snapshot-prune",
         ["snapshot", "prune", "--older-than", "1h", "--name-prefix", "pre-", "--vms", "810-812", "-p", "{profile}"],
         "The pre- snapshots older than an hour, listed then deleted once confirmed (run an hour after guests-snapshot-add)",
         stdin="y\n", echo=YES, width=100, manual=True),
    # Removal (run last: they delete the test guests)
    Shot("guests-vm-remove", ["vm", "remove", "802,803", "--force", "--purge", "-p", "{profile}"],
         "Two VMs deleted, the running one stopped first", stdin="y\n", echo=YES, manual=True),
    Shot("guests-ct-remove", ["ct", "remove", "806,807", "--force", "--purge", "-y", "-p", "{profile}"],
         "Two running containers stopped and deleted, without the question", manual=True),
]

# The vm add wizard, answered down to its summary, then refused (n): the
# recording creates nothing, so it can run with the others
CASTS = [
    Cast("guests-vm-add-wizard", ["vm", "add", "pve1", "-p", "{profile}"], [
        (2.5, "804"), (0.6, ENTER),          # VMID
        (1.0, "app-02"), (0.6, ENTER),       # name
        (1.5, ENTER),                        # pool: (none)
        (1.5, SPACE), (0.8, ENTER),          # tags: web
        (1.2, ENTER),                        # start at boot: n
        (1.5, ENTER),                        # ISO storage
        (2.0, ENTER),                        # ISO
        (1.5, ENTER),                        # OS type
        (1.5, ENTER),                        # kernel
        (1.2, ENTER), (1.0, "2"), (0.6, ENTER), (1.0, ENTER),   # sockets, cores, vCPUs
        (1.5, ENTER),                        # CPU type
        (1.2, ENTER),                        # memory
        (1.2, ENTER),                        # add a disk: y
        (1.5, ENTER),                        # disk storage
        (1.2, "16"), (0.6, ENTER),           # disk size
        (1.5, ENTER),                        # format
        (1.5, ENTER),                        # bridge
        (1.2, ENTER), (1.0, ENTER), (1.0, ENTER),   # VLAN, firewall, disconnected
        (2.5, "n"), (0.6, ENTER),            # create: n
    ], rows=30, manual=True),
    # vm start without an ID: the multi-select menu of the VMs (800 and 801
    # are empty stopped VMs on Ceph, the first VMs after 300 and 301)
    Cast("guests-vm-start-menu", ["vm", "start", "-p", "{profile}"], [
        (3.0, DOWN), (0.6, DOWN), (0.8, SPACE),   # 800
        (0.8, DOWN), (0.8, SPACE),                # 801
        (1.5, ENTER),
    ], rows=16, manual=True),
    # vm migrate without a target: the menu of the other online nodes, then
    # the plan and a live migration (800 running, its disk on Ceph)
    Cast("guests-vm-migrate-menu", ["vm", "migrate", "800", "-p", "{profile}"], [
        (3.5, DOWN), (1.5, ENTER),                # pve3
        (2.5, ENTER),                             # start the migration: y
    ], rows=16, manual=True),
    # tag add without --color: the palette of tag_color.yml (sets the color
    # of web on the cluster: remove it after with tag remove web). Run it with
    # HOME holding the default palette (tag color init) and links to the rest
    # of ~/.config/pvecli, so that no color of your own shows
    Cast("guests-tag-color-menu", ["tag", "add", "web", "-p", "{profile}"], [
        (2.5, DOWN), (0.6, DOWN), (0.6, DOWN), (1.5, ENTER),
    ], rows=24, manual=True),
    # The ct add wizard down to its summary, then refused (n). Needs the tag
    # web (guests-tag-color-menu) and no resource pool on the cluster
    Cast("guests-ct-add-wizard", ["ct", "add", "pve1", "-p", "{profile}"], [
        (2.5, "805"), (0.6, ENTER),          # CTID
        (1.0, "proxy-03"), (0.6, ENTER),     # hostname
        (1.5, SPACE), (0.8, ENTER),          # tags: web
        (1.2, ENTER),                        # start at boot: n
        (1.5, ENTER),                        # image storage
        (2.0, ENTER),                        # image
        (1.2, "secret1"), (0.4, ENTER),      # root password (not echoed)
        (1.0, "secret1"), (0.4, ENTER),      # confirmation
        (1.2, ENTER), (1.0, ENTER), (1.0, ENTER),   # cores, RAM, swap
        (1.5, ENTER),                        # root filesystem storage
        (1.2, "4"), (0.6, ENTER),            # size
        (1.5, ENTER),                        # bridge
        (1.5, ENTER),                        # IPv4: DHCP
        (1.5, DOWN), (0.4, DOWN), (0.4, DOWN), (0.8, ENTER),   # IPv6: none
        (1.2, ENTER), (1.0, ENTER),          # VLAN, firewall
        (1.2, ENTER), (1.0, ENTER),          # keyctl, FUSE
        (2.5, "n"), (0.6, ENTER),            # create: n
    ], rows=30, manual=True),
]
