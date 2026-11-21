#!/bin/sh
# Install and start the AlphaLab tick timer. Run as: sudo /home/artem/alphalab/deploy/install.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo $HERE/install.sh" >&2
    exit 1
fi
install -m 0644 "$HERE/alphalab-tick.service" /etc/systemd/system/alphalab-tick.service
install -m 0644 "$HERE/alphalab-tick.timer" /etc/systemd/system/alphalab-tick.timer
systemctl daemon-reload
systemctl enable --now alphalab-tick.timer
echo
systemctl list-timers alphalab-tick.timer --no-pager
echo
echo "Clock (must say 'System clock synchronized: yes'):"
timedatectl | grep -E "synchronized|NTP service|Time zone" || true
echo
echo "Logs:   journalctl -u alphalab-tick.service -f"
echo "Stop:   sudo systemctl disable --now alphalab-tick.timer"
