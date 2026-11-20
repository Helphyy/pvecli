---
icon: lucide/tags
title: Organize guests with tags and pools
description: Tag guests and color the tags, rename or remove a tag on every guest, copy tags and pools to another cluster, and group guests in resource pools.
---

# Organize guests with tags and pools

Tags are free labels on guests, shown in the Proxmox VE web interface and used by the `--tag` filter of many pvecli commands. A pool is a named group of guests; a guest belongs to one pool at most, and pools also carry permissions (see [Permissions](../reference/permissions.md)).

## Tag a guest

[vm tag add](../cli/vm/tag.md#add) adds one tag or several, comma separated, to what the VM already has (`--replace` sets them instead):

--8<-- "screens/guests-vm-tag-add.html"

--8<-- "screens/guests-ct-tag-add.html"

`vm tag remove` and `vm tag list` take off and show the tags of one VM; [ct tag](../cli/ct/tag.md) does the same for containers. A tag can also be set at creation, with `--tags` of `vm deploy`, `ct deploy` and the deploy file.

## Every tag of the cluster

[tag list](../cli/tag/list.md) counts the guests carrying each tag:

--8<-- "screens/guests-tag-list.html"

A color is a cluster setting, shared by all guests carrying the tag. [tag add](../cli/tag/add.md) sets it:

--8<-- "screens/guests-tag-color.html"

Without `--color`, pvecli shows a palette to pick from:

<div class="cast" data-src="../../assets/casts/guests-tag-color-menu.cast" data-poster="npt:0:5.8" data-rows="13"></div>

The palette is a local file, `~/.config/pvecli/tag_color.yml`: `pvecli tag color init` creates it with default colors, `tag color add` and `tag color remove` edit it.

## Rename or remove a tag everywhere

[tag edit](../cli/tag/edit.md) renames a tag on every guest that carries it, and keeps its color:

--8<-- "screens/guests-tag-edit.html"

Renaming to a tag that already exists merges the two, after a question. `--color` changes the color at the same time.

[tag remove](../cli/tag/remove.md) takes a tag (or several, comma separated) off every guest, and drops its color, after listing the guests it touches:

--8<-- "screens/guests-tag-remove.html"

## Copy tag colors to another cluster

[tag export](../cli/tag/export.md) writes the tags of a cluster and their colors to a JSON file, and [tag import](../cli/tag/import.md) applies the colors of that file to another cluster:

--8<-- "screens/guests-tag-export.html"

--8<-- "screens/guests-tag-import.html"

Only the colors are imported: the four tags of the file became one imported tag, `api`, the only one with a color. A tag exists on a cluster through the guests that carry it. When a tag already has a color on the target, pvecli asks before overwriting it; `--force` skips those tags without asking.

## Group guests in a pool

Create the pool with [pool add](../cli/pool/add.md), then put guests in it with [pool content add](../cli/pool/content.md#add):

--8<-- "screens/guests-pool-add.html"

--8<-- "screens/guests-pool-content-add.html"

A guest already in another pool is refused; `--force` moves it. `pool content remove` takes guests out, and `--pool` of `vm add`, `vm clone`, `vm deploy`, `ct deploy` puts a new guest in a pool directly.

[pool usage](../cli/pool/usage.md) adds up what the guests of the pool use and what they were given, and compares it with the cluster:

--8<-- "screens/guests-pool-usage.html"

- `0.54 cores used of 6 vCPU allocated`: the CPU the pool's guests use now, against the vCPUs they were given.
- `Share of the cluster hardware`: the pool against the cores, memory and guest storage of the nodes.
- `Share of all guests`: the pool against every guest of the cluster.

[pool info](../cli/pool/info.md) lists the members of a pool, [pool list](../cli/pool/list.md) every pool. [pool remove](../cli/pool/remove.md) deletes a pool. A pool that still has members needs `--force`: its guests leave the pool and are not deleted.

## Copy pools to another cluster

[pool export](../cli/pool/export.md) writes the pools of a cluster (names and comments, not members) to a JSON file, and [pool import](../cli/pool/import.md) creates them on another cluster:

--8<-- "screens/guests-pool-export.html"

--8<-- "screens/guests-pool-import.html"

A pool that already exists on the target is skipped (pvecli offers to update its comment); `--force` skips without asking.
