fixture agent: relevée sur la VM 320 (Debian 13 + qemu-guest-agent, PVE 9.1.1) le 2026-11-18
sdn_lock_held: réponse réelle de POST /cluster/sdn/lock quand une autre session tient le verrou (relevée le 2026-11-18)
backup_vzdump_all_guests_external: réponse réelle de POST /nodes/pve-lab1/vzdump vmid=300 alors que la VM 300 est sur pve-lab2 : la chaîne "OK", sans tâche ni UPID (relevée le 2026-11-18)
backup_vzdump_log_external_guest_dropped: journal réel de vzdump 300 919 lancé sur pve-lab1 (300 est sur pve-lab2, 919 n'existe pas) : 300 n'apparaît nulle part dans le journal (relevé le 2026-11-18)
