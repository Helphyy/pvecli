---
icon: lucide/book-open
title: Reference
description: The files pvecli reads, the Proxmox VE privileges its commands need, and its exit codes.
---

# Reference

What the task pages rely on: the files pvecli reads, the Proxmox VE privileges behind its commands, and its exit codes for scripts. The options of each command are in the command reference.

<div class="grid cards" markdown>

-   :lucide-file-cog:{ .lg } __Configuration file__

    ---

    Every key of `config.yaml`: profiles, groups, encrypted secrets.

    [:lucide-arrow-right: Configuration file](config-file.md)

-   :lucide-file-text:{ .lg } __Deploy file format__

    ---

    Every key of the YAML file read by `pvecli deploy`.

    [:lucide-arrow-right: Deploy file format](deploy-file.md)

-   :lucide-lock:{ .lg } __Permissions__

    ---

    The Proxmox VE privileges each command needs, and what pvecli checks itself.

    [:lucide-arrow-right: Permissions](permissions.md)

-   :lucide-hash:{ .lg } __Exit codes__

    ---

    The exit status of each command, for scripts and CI jobs.

    [:lucide-arrow-right: Exit codes](exit-codes.md)

-   :lucide-square-terminal:{ .lg } __Command reference__

    ---

    Every command and option, generated from the code.

    [:lucide-arrow-right: All commands](../cli/index.md)

</div>
