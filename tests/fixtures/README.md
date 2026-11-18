fixture agent: relevée sur la VM 320 (Debian 13 + qemu-guest-agent, PVE 9.1.1) le 2026-11-18
sdn_lock_held: réponse réelle de POST /cluster/sdn/lock quand une autre session tient le verrou (relevée le 2026-11-18)
backup_vzdump_all_guests_external: réponse réelle de POST /nodes/pve-lab1/vzdump vmid=300 alors que la VM 300 est sur pve-lab2 : la chaîne "OK", sans tâche ni UPID (relevée le 2026-11-18)
backup_vzdump_log_external_guest_dropped: journal réel de vzdump 300 919 lancé sur pve-lab1 (300 est sur pve-lab2, 919 n'existe pas) : 300 n'apparaît nulle part dans le journal (relevé le 2026-11-18)
pve-8.4: relevé le 2026-11-18 par `python -m tests.tools.record_fixtures pve8a pve-8.4` sur le cluster pve8lab (PVE 8.4.21, nodes pve8a et pve8b, VMs 990 et 991 de npx-cafedeca), groupe HA prefa avec vm:120 et vm:125. Les 501 (bulk_action_guest, ha_rules, sdn_dry_run, lxc_migrate_preconditions) sont les endpoints absents de PVE 8
pve-8.4/qemu_migrate_preconditions_local_cdrom: réponse réelle de GET /nodes/pve8b/qemu/110/migrate?target=pve8a quand la VM 110 gardait un ISO de `local` en ide2 (et un snapshot qui le référence) : PVE refuse alors toute migration (« local cdrom image »)
