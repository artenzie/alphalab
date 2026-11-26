#!/bin/sh
# Install and start the AlphaLab timers (the 5-minute tick, the 02:30 UTC nightly backup) and the
# read-only status page on the Tailscale address.
# Run as: sudo /home/artem/alphalab/deploy/install.sh   (safe to run again after a change)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo $HERE/install.sh" >&2
    exit 1
fi
for unit in alphalab-tick.service alphalab-tick.timer alphalab-backup.service alphalab-backup.timer \
            alphalab-status.service; do
    install -m 0644 "$HERE/$unit" "/etc/systemd/system/$unit"
done
chmod 0755 "$HERE/backup.sh"
systemctl daemon-reload
systemctl enable --now alphalab-tick.timer alphalab-backup.timer alphalab-status.service
systemctl restart alphalab-status.service      # pick up a changed page or unit
echo
systemctl list-timers 'alphalab-*' --no-pager
echo
echo "Clock (must say 'System clock synchronized: yes'):"
timedatectl | grep -E "synchronized|NTP service|Time zone" || true
echo
echo "Status page:"
systemctl is-active alphalab-status.service || true
IP=$(ip -4 -o addr show tailscale0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
echo "  http://${IP:-<tailscale0 has no address>}:8080/"
echo
echo "Logs:   journalctl -u alphalab-tick.service -f"
echo "        journalctl -u alphalab-backup.service"
echo "        journalctl -u alphalab-status.service"
echo "Backup now (test):  sudo systemctl start alphalab-backup.service"
echo "Stop:   sudo systemctl disable --now alphalab-tick.timer alphalab-backup.timer alphalab-status.service"
