"""Captures of the Backups section (docs/backups/).

Test guests: 820 dns-01 and 821 proxy-01 (containers on pve1), 822 web-02
(VM on pve2), all created for these captures and removed afterwards. Every
shot that writes is manual: run it by name once the guests exist.
"""

from scripts.docs.screens import Shot

YES = [("(n): ", "y")]
NO = [("(n): ", "n")]

SHOTS = [
    Shot("backups-run", ["backup", "run", "820-822", "--storage", "pbs", "-p", "{profile}"],
         "Three guests on two nodes backed up to PBS, one vzdump per node, each checked",
         stdin="y\n", echo=YES, marks=["pve1", "pve2", "3/3 guest(s) backed up"], manual=True),
    Shot("backups-run-protected", ["backup", "run", "820", "--protected", "--notes", "before PHP 8.4", "-y",
                                   "-p", "{profile}"],
         "One guest to the node's default storage, protected, with notes", width=100,
         marks=["1/1 guest(s) backed up"], manual=True),

    # Coverage (needs the nightly job of backups-jobs-add)
    Shot("backups-status", ["backup", "status", "-p", "{profile}"],
         "Covering job, last backup and last run of each guest", width=100, manual=True),
    Shot("backups-status-older", ["backup", "status", "--older-than", "1d", "--failed", "-p", "{profile}"],
         "Only the guests with an old or missing backup, or a failed run", width=100, manual=True),
    Shot("backups-list", ["backup", "list", "--vmid", "820-822", "-p", "{profile}"],
         "Backups of three guests, newest first per guest, on PBS and on local", width=120, manual=True),
    Shot("backups-find-no-backup", ["find", "--no-backup", "-p", "prod,staging"],
         "Guests no backup job covers, on two clusters", width=100, manual=True),

    # Restore
    Shot("backups-restore-new", ["backup", "restore", "pbs:backup/vm/822/2026-11-19T09:01:07Z", "--vmid", "830",
                                 "--unique", "-p", "{profile}"],
         "A VM backup restored as a new VM with new MAC addresses",
         stdin="y\n", echo=YES, marks=["Restored as VM 830"], manual=True),
    Shot("backups-restore-ct", ["backup", "restore", "local:backup/vzdump-lxc-821-2026_11_19-09_59_25.tar.zst",
                                "--vmid", "831", "--start", "-p", "{profile}"],
         "A container backup restored as 831, on the storage of its root disk, then started",
         stdin="y\n", echo=YES, marks=["running"], manual=True),
    Shot("backups-restore-exists", ["backup", "restore", "pbs:backup/vm/822/2026-11-19T09:01:07Z", "--vmid", "822",
                                    "-p", "{profile}"],
         "Refused: the ID is taken and --force is not given", manual=True),
    Shot("backups-restore-force", ["backup", "restore", "local:backup/vzdump-lxc-820-2026_11_19-09_52_00.tar.zst",
                                   "--vmid", "820", "--force", "-p", "{profile}"],
         "A stopped container overwritten by its backup, the ID typed again to confirm",
         stdin="820\n", echo=[("to confirm: ", "820")], marks=["Restored as CT 820"], manual=True),

    # Files inside a PBS backup
    Shot("backups-files-root", ["backup", "files", "pbs:backup/ct/821/2026-11-19T09:01:54Z", "-p", "{profile}"],
         "Top of a container backup on PBS: one archive", manual=True),
    Shot("backups-files-dir", ["backup", "files", "pbs:backup/ct/821/2026-11-19T09:01:54Z", "/root.pxar.didx/etc/network", "-p", "{profile}"],
         "A directory inside the archive", manual=True),
    Shot("backups-files-file", ["backup", "files", "pbs:backup/ct/821/2026-11-19T09:01:54Z", "/root.pxar.didx/etc/network/interfaces",
                                "-p", "{profile}"],
         "One file: its size and date", manual=True),
    Shot("backups-files-local", ["backup", "files", "local:backup/vzdump-lxc-821-2026_11_19-09_59_25.tar.zst",
                                 "-p", "{profile}"],
         "Refused on a backup that is not on PBS", manual=True),

    # Prune, protect, notes, remove (backups of 820 on local)
    Shot("backups-prune-no-retention", ["backup", "prune", "local", "--vmid", "820", "-p", "{profile}"],
         "A storage without retention needs --keep-*", manual=True),
    Shot("backups-prune-dry", ["backup", "prune", "local", "--keep-last", "1", "--vmid", "820", "--dry-run",
                               "-p", "{profile}"],
         "PVE's decision for each backup, nothing removed", width=104, marks=["remove"], manual=True),
    Shot("backups-protect", ["backup", "protect", "local:backup/vzdump-lxc-820-2026_11_19-09_53_19.tar.zst", "-p", "{profile}"],
         "One backup protected", manual=True),
    Shot("backups-notes", ["backup", "notes", "local:backup/vzdump-lxc-820-2026_11_19-09_53_19.tar.zst", "before the PHP 8.4 upgrade", "-p", "{profile}"],
         "Notes set on a backup", manual=True),
    Shot("backups-prune", ["backup", "prune", "local", "--keep-last", "1", "--vmid", "820", "-p", "{profile}"],
         "The protected backup is kept, the other one removed after confirmation",
         stdin="y\n", echo=YES, width=104, marks=["protected", "1 backup(s) pruned"], manual=True),
    Shot("backups-list-protected", ["backup", "list", "--vmid", "820", "--storage", "local", "-p", "{profile}"],
         "The protected backup of backups-run-protected, with its notes", width=120, manual=True),
    Shot("backups-remove-protected", ["backup", "remove", "local:backup/vzdump-lxc-820-2026_11_19-09_53_19.tar.zst", "-p", "{profile}"],
         "A protected backup is never deleted", manual=True),
    Shot("backups-unprotect", ["backup", "unprotect", "local:backup/vzdump-lxc-820-2026_11_19-09_53_19.tar.zst", "-p", "{profile}"],
         "Protection removed", manual=True),
    Shot("backups-remove", ["backup", "remove", "local:backup/vzdump-lxc-820-2026_11_19-09_53_19.tar.zst", "-p", "{profile}"],
         "A backup deleted after confirmation", stdin="y\n", echo=YES, marks=["1/1 backup(s) deleted"],
         manual=True),

    # Jobs
    Shot("backups-jobs-add", ["backup", "jobs", "add", "--id", "nightly", "--schedule", "02:00",
                              "--vmid", "820,821", "--storage", "pbs", "--keep-daily", "7",
                              "--keep-weekly", "4", "-p", "{profile}"],
         "A job created with its own retention", manual=True),
    Shot("backups-jobs-list", ["backup", "jobs", "list", "-p", "{profile}"],
         "The jobs with schedule, guests, retention and next run", width=124, manual=True),
    Shot("backups-jobs-edit", ["backup", "jobs", "edit", "nightly", "--vmid", "820,821,822",
                               "--schedule", "mon..fri 01:30", "-p", "{profile}"],
         "Only the given options change", manual=True),
    Shot("backups-jobs-run", ["backup", "jobs", "run", "nightly", "-p", "{profile}"],
         "A job run now with its own options, each guest checked",
         stdin="y\n", echo=YES, marks=["guest(s) backed up"], manual=True),
    Shot("backups-jobs-disable", ["backup", "jobs", "disable", "nightly", "-p", "{profile}"],
         "A job kept but no longer scheduled", manual=True),
    Shot("backups-jobs-list-disabled", ["backup", "jobs", "list", "-p", "{profile}"],
         "A disabled job: no next run", width=124, marks=["disabled"], manual=True),
    Shot("backups-jobs-remove", ["backup", "jobs", "remove", "nightly", "-p", "{profile}"],
         "A job deleted after confirmation; its backups stay",
         stdin="y\n", echo=YES, manual=True),
]
