#!/bin/bash
# Nightly AlphaLab backup (alphalab-backup.timer, 02:30 UTC):
#   every data/*.db (not the alphalab-backup-* copies) -> SQLite online backup -> integrity_check
#   -> gzip (runner/backup.py make) -> rclone copy to gdrive:alphalab-backups/YYYY-MM-DD/
#   -> purge dated folders older than KEEP_DAYS. Any failure: a phone alert (runner.alerts notify)
#   and a 'failed' marker; success: an 'ok' marker. The daily summary shows the marker.
# rclone's config (remote 'gdrive') lives in ~/.config/rclone/rclone.conf, never in the repo.
set -Eeuo pipefail

REPO=/home/artem/alphalab
PY="$REPO/venv/bin/python"
REMOTE=gdrive:alphalab-backups
KEEP_DAYS=30
DAY=$(date -u +%F)
STAGE="$HOME/.cache/alphalab-backup/$DAY"
STEP=start

cd "$REPO"

fail() {
    local code=$?
    echo "backup FAILED at step: $STEP (exit $code)" >&2
    "$PY" -m runner.backup marker failed "$DAY" "$STEP" || true
    "$PY" -m runner.alerts notify --key "backup:$DAY" --priority 4 --title "Backup FAILED $DAY" \
        "Nightly backup failed at: $STEP (exit $code). See: journalctl -u alphalab-backup.service" || true
    exit "$code"
}
trap fail ERR

STEP="make (online backup, integrity_check, gzip)"
rm -rf "$STAGE"
"$PY" -m runner.backup make "$STAGE"

STEP="rclone copy"
rclone copy "$STAGE" "$REMOTE/$DAY" --checksum --retries 3 --low-level-retries 10 --stats-one-line -v

STEP="rclone check"
rclone check "$STAGE" "$REMOTE/$DAY" --one-way

STEP="retention (folders older than $KEEP_DAYS days)"
CUTOFF=$(date -u -d "-$KEEP_DAYS days" +%F)
rclone lsf --dirs-only "$REMOTE" | sed 's#/$##' | while read -r d; do
    if [[ "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && "$d" < "$CUTOFF" ]]; then
        echo "purging $REMOTE/$d (older than $CUTOFF)"
        rclone purge "$REMOTE/$d"
    fi
done

STEP="cleanup"
rm -rf "$STAGE"
"$PY" -m runner.backup marker ok "$DAY"
echo "backup $DAY: ok"
