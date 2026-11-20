---
icon: lucide/hash
title: Exit codes
description: The exit status of pvecli commands, for scripts and CI jobs.
---

# Exit codes

| Code | When |
|:--|:--|
| `0` | The command succeeded. Also when you answer `n` to a confirmation, after `--help` on a command, and for `--version`. |
| `1` | The command failed: unknown profile, guest not found, API error, refused permission. Also when a prompt gets no input (end of file on stdin, shown as `Aborted.`), when a typed confirmation (a VMID or a cluster name to type again) does not match, and in the cases below. |
| `2` | Wrong usage: unknown command or option, missing argument, or `pvecli` or a command group run without a subcommand (its help is printed). Also [audit](../cli/audit.md) on one profile when it reports at least one `FAIL`. |
| `130` | Interrupted with ++ctrl+c++, in a menu, at a prompt or while the command waits for Proxmox. pvecli prints `Cancelled`. |
| `255` | `vm ssh`, `ct ssh` or `node ssh` could not connect. |
| other | `vm ssh`, `ct ssh` and `node ssh` exit with the status of `ssh`: that of the remote shell, or of the command given with `-c`. |

## Several profiles

A command run on several profiles (see [Run a command on several clusters](../fleet/several-clusters.md)) goes on after a failing profile, then exits with one status for all of them: `130` if one was interrupted, `1` if one could not run or failed, and `2` when every failure was a `FAIL` found by [audit](../cli/audit.md):

--8<-- "screens/reference-fleet-failure.html"

The line `Failed on 1/2 profile(s): staging` is printed on stderr, after every profile has run.

With `--json`, the document holds the error and the exit code of each failed profile, and the command exits by the same rule:

--8<-- "screens/reference-fleet-failure-json.html"

The last line comes from stderr: stdout holds only the JSON document.

## Commands with their own rules

| Command | Exit code |
|:--|:--|
| [deploy](../cli/deploy.md) | `1` when the file has a problem or a conflict with the cluster (nothing is created), or when a guest fails. `0` after a `--dry-run` that finds neither, when every guest is already present, or when you answer `n`. |
| [vm exec](../cli/vm/exec.md) | `1` when the command fails on at least one VM. The command's own exit code is printed, not returned. |
| [audit](../cli/audit.md) | `2` when at least one check is `FAIL`, with or without `--json`. `WARN` and `INFO` findings exit `0`. On several profiles too, unless a profile could not run: then `1`. |

In a script, test the exit code and read the details from `--json` (see [Use pvecli in scripts](../fleet/scripting.md)):

```bash
pvecli audit -p prod --json > audit.json || echo "audit failed with status $?"
```
