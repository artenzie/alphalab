#!/bin/sh
# Install and start the AlphaLab timers: the 5-minute tick and the 02:30 UTC nightly backup.
# Run as: sudo /home/artem/alphalab/deploy/install.sh   (safe to run again after a change)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo $HERE/install.sh" >&2
    exit 1
fi
for unit in alphalab-tick.service alphalab-tick.timer alphalab-backup.service alphalab-backup.timer; do
    install -m 0644 "$HERE/$unit" "/etc/systemd/system/$unit"
done
chmod 0755 "$HERE/backup.sh"
systemctl daemon-reload
systemctl enable --now alphalab-tick.timer alphalab-backup.timer
echo
systemctl list-timers 'alphalab-*' --no-pager
echo
echo "Clock (must say 'System clock synchronized: yes'):"
timedatectl | grep -E "synchronized|NTP service|Time zone" || true
echo
echo "Logs:   journalctl -u alphalab-tick.service -f"
echo "        journalctl -u alphalab-backup.service"
echo "Backup now (test):  sudo systemctl start alphalab-backup.service"
echo "Stop:   sudo systemctl disable --now alphalab-tick.timer alphalab-backup.timer"
