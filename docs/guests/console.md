---
icon: lucide/terminal
title: Open a console or a shell
description: Open the VNC console of a guest in your browser, SSH or RDP into it with its address found for you, or run a command in VMs through the guest agent.
---

# Open a console or a shell

| Command | Needs | Works for |
|:--|:--|:--|
| `vm vnc`, `ct vnc` | The guest running | VMs and containers, with or without network |
| `vm ssh` | The QEMU guest agent running in the VM, and SSH reachable from your machine | VMs |
| `ct ssh` | The container running with an address, and SSH reachable | Containers |
| `vm rdp` | The guest agent, an RDP client on your machine | Windows VMs |
| `vm exec` | The guest agent | VMs |

## VNC console

[vm vnc](../cli/vm/vnc.md) opens the console of a VM in your browser, the same noVNC console as in the Proxmox VE web interface, without opening the web interface:

```bash
pvecli vm vnc 810 -p prod
```

```text
✓ VNC console for VM 810 (api-01) running in background (PID: 48213)
```

pvecli asks Proxmox VE for a console ticket with your profile's credentials, starts a small server on `localhost` that serves noVNC and relays the console, and opens its page. The server stops a few seconds after you close the tab, or after five minutes if no browser connects. Several IDs open one tab each; `--split` opens separate windows. With `--no-background`, the server runs in the foreground until you press ++enter++.

[ct vnc](../cli/ct/vnc.md) does the same for a container. The VNC console is the way into a guest that has no network or no guest agent.

## SSH into a VM

[vm ssh](../cli/vm/ssh.md) asks the guest agent for the VM's address, then runs `ssh` to it:

```bash
pvecli vm ssh 810 -u debian -p prod
```

```text
Connecting to debian@10.0.0.24...
```

`-c` runs one command instead of a shell. `-i` gives the private key, `-P` the port. `-j` jumps through the node the profile connects to, for VMs on a network your machine does not reach. The user, port and key default to the `ssh_user` (else `root`), `ssh_port` and `ssh_key` of the profile (see [Configuration file](../reference/config-file.md)).

pvecli passes `StrictHostKeyChecking=no` and `UserKnownHostsFile=/dev/null` to `ssh`: the host key of the guest is neither checked nor saved in your `known_hosts`, and `ssh` prints a `Permanently added` warning at each connection.

Without a guest agent, pvecli cannot find the address and refuses:

--8<-- "screens/guests-vm-ssh-noagent.html"

## SSH into a container

[ct ssh](../cli/ct/ssh.md) reads the container's address from Proxmox VE, no agent needed, and takes the same options. The container must run an SSH server:

```bash
pvecli ct ssh 812 -c hostname -p prod
```

```text
Connecting to root@10.0.0.26...
Warning: Permanently added '10.0.0.26' (ED25519) to the list of known hosts.
dns-02
```

## RDP into a Windows VM

[vm rdp](../cli/vm/rdp.md) finds the VM's address through the guest agent, asks for the password (and the domain unless `-d` is given), then starts the RDP client: `xfreerdp3`, `xfreerdp` or `rdesktop` on Linux, Microsoft Remote Desktop on macOS, `mstsc` on Windows.

```bash
pvecli vm rdp 815 -u Administrator -p prod
```

`-f` opens it full screen, `-r 1920x1080` sets a fixed resolution, `-m ~/Documents` shares a local folder (FreeRDP and rdesktop only), `-j` goes through an SSH tunnel to the node. The user and port default to `rdp_user` and `rdp_port` of the profile.

## Run a command in VMs

[vm exec](../cli/vm/exec.md) runs a command through the guest agent, without SSH or network access to the VM. Put the command after `--`:

--8<-- "screens/guests-vm-exec.html"

- The command runs in `sh -c` on Linux, `cmd /c` on Windows; `-s bash` or `-s powershell` changes that.
- pvecli waits 30 seconds for the command to end (`-t` to change).
- With several VMs, each output gets a header and the summary counts the failures. `--tag`, `--pool` and `--node` select the VMs, as in [Start, stop and reboot guests](power.md).
- The exit code is 1 when the command fails in at least one VM.

On a VM whose guest agent does not answer:

--8<-- "screens/guests-vm-exec-noagent.html"

Install `qemu-guest-agent` in the VM and check that the agent is enabled in its options (`vm add` enables it by default).
