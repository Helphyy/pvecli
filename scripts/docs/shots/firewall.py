"""Captures of the firewall pages (Guests, Infrastructure, Fleet).

They run on a single node lab, VM 890 (two cards) and container 891 (one
card), with the firewall empty and off everywhere at the start. Before
guests-firewall-rule-add, VM 890 gets two rules (vm firewall rule add 890
-ac ACCEPT -ma HTTPS -c "public site", then -ac ACCEPT -pt icmp -c ping):

    export PVECLI_SCREENS_CONF=scripts/docs/screens.fw.local.toml
    .venv/bin/python -m scripts.docs.screens NAME...

All are manual and change the lab: run them in the order of this list,
which is the order of the pages (Guests, then Infrastructure, then Fleet).
infrastructure-firewall-enable turns the datacenter firewall on: check a
way in that does not go through the API first (SSH as root, the console).
fleet-firewall-import-run leaves the firewall of docs/fleet/firewall.yaml;
end with `cluster firewall import` of an empty file, then `cluster firewall
disable`, to leave the lab as it was.
"""

from scripts.docs.screens import Shot

YES = [("(n): ", "y")]
NO = [("(n): ", "n")]

SHOTS = [
    # Guests: the firewall of a guest
    Shot("guests-firewall-status-off", ["ct", "firewall", "status", "891", "-p", "{profile}"],
         "A container whose firewall is off and whose card is not flagged",
         marks=["net0 not filtered"], manual=True),
    Shot("guests-firewall-enable-nics", ["ct", "firewall", "enable", "891", "--nics", "-p", "{profile}"],
         "The firewall of a container turned on, its card flagged",
         stdin="y\n", echo=YES, marks=["net0"], manual=True),
    Shot("guests-firewall-rule-add",
         ["vm", "firewall", "rule", "add", "890", "-ac", "ACCEPT", "-ma", "SSH", "-src", "10.0.0.0/24", "-c", "admins",
          "-p", "{profile}"],
         "A rule added to a VM, shown before it is written",
         stdin="y\n", echo=YES, marks=["SSH"], manual=True),
    Shot("guests-firewall-rule-list", ["vm", "firewall", "rule", "list", "890", "-p", "{profile}"],
         "The rules of the VM in the order they are evaluated", marks=["HTTPS"], manual=True),
    Shot("guests-firewall-status-dc-off", ["vm", "firewall", "status", "890", "-p", "{profile}"],
         "A VM whose firewall is on, not enforced while the datacenter is off",
         marks=["Enforced"], manual=True),
    # Infrastructure: the firewall of the cluster
    Shot("infrastructure-firewall-status", ["cluster", "firewall", "status", "-p", "{profile}"],
         "Every level of the cluster, before the datacenter firewall is on",
         marks=["Enforced"], manual=True),
    Shot("infrastructure-firewall-options", ["cluster", "firewall", "options", "-p", "{profile}"],
         "The options of the datacenter, with the defaults of Proxmox VE", manual=True),
    Shot("infrastructure-firewall-management",
         ["cluster", "firewall", "ipset", "add", "management", "10.0.0.7", "-c", "admin workstation",
          "-y", "-p", "{profile}"],
         "The management IP set created with the address of this machine", manual=True),
    Shot("infrastructure-firewall-enable", ["cluster", "firewall", "enable", "-p", "{profile}"],
         "The datacenter firewall turned on: the access checked before and after",
         stdin="y\n", echo=YES, marks=["Access kept", "still works"], manual=True),
    Shot("infrastructure-firewall-refused",
         ["node", "firewall", "rule", "add", "pve1", "-ac", "DROP", "-pt", "tcp", "-dpt", "8006",
          "-y", "-p", "{profile}"],
         "A node rule that would close the API to this machine, refused",
         marks=["closes the API"], manual=True),
    Shot("infrastructure-firewall-group-info", ["cluster", "firewall", "group", "info", "webserver", "-p", "{profile}"],
         "A security group: its rules and the levels that insert it", manual=True),
    Shot("infrastructure-firewall-status-on", ["cluster", "firewall", "status", "-p", "{profile}"],
         "Every level of the cluster once the datacenter firewall is on", manual=True),
    Shot("guests-firewall-status-on", ["vm", "firewall", "status", "890", "-p", "{profile}"],
         "The firewall of the VM, enforced", marks=["Enforced"], manual=True),
    # Fleet: the firewall in a file
    Shot("fleet-firewall-export", ["cluster", "firewall", "export", "-p", "{profile}"],
         "The whole firewall of the cluster as YAML", manual=True),
    Shot("fleet-firewall-import-plan", ["cluster", "firewall", "import", "docs/fleet/firewall.yaml", "-p", "{profile}"],
         "The plan of an import, answered no", stdin="n\n", echo=NO, manual=True),
    Shot("fleet-firewall-import-run",
         ["cluster", "firewall", "import", "docs/fleet/firewall.yaml", "-y", "-p", "{profile}"],
         "The import applied, the access checked after", marks=["made"], manual=True),
    Shot("fleet-firewall-import-again",
         ["cluster", "firewall", "import", "docs/fleet/firewall.yaml", "-p", "{profile}"],
         "A second import: nothing to change", manual=True),
]
