"""runner/backup.py: online backup, integrity check, gzip, manifest, marker, verify (the drill's checks)."""

import gzip
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data import db  # noqa: E402
from runner import alerts, backup  # noqa: E402


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    for name in ("dev.db", "official.db"):
        c = db.init_db(d / name)
        with c:
            c.execute("INSERT INTO heartbeat (run_id, job, event) VALUES ('r', 'tick', 'finished')")
        c.close()                                    # WAL left behind, like the live files
    db.init_db(d / "alphalab-backup-2026-11-17.db").close()
    return d


def test_make_backs_up_every_db_but_the_backup_copies(data_dir, tmp_path):
    out = tmp_path / "stage"
    man = backup.make(out, data_dir)
    assert sorted(man["files"]) == ["dev.db.gz", "official.db.gz"]
    assert sorted(p.name for p in out.iterdir()) == ["dev.db.gz", "manifest.json", "official.db.gz"]
    assert json.loads((out / "manifest.json").read_text()) == man
    restored = tmp_path / "r.db"
    restored.write_bytes(gzip.decompress((out / "dev.db.gz").read_bytes()))
    assert backup.integrity(restored) == "ok"
    assert backup.row_counts(restored)["heartbeat"] == 1 == man["files"]["dev.db.gz"]["rows"]["heartbeat"]
    assert man["files"]["dev.db.gz"]["rows"]["agents"] == 17


def test_a_corrupt_database_fails_the_backup(data_dir, tmp_path):
    (data_dir / "broken.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.make(tmp_path / "stage", data_dir)


def test_verify_passes_and_allows_live_to_be_ahead(data_dir, tmp_path):
    out = tmp_path / "stage"
    backup.make(out, data_dir)
    c = db.connect(data_dir / "dev.db")
    with c:
        c.execute("INSERT INTO heartbeat (run_id, job, event) VALUES ('r2', 'tick', 'finished')")
    c.close()
    lines = backup.verify(out, data_dir)
    assert any("dev.db: integrity ok" in ln for ln in lines)
    assert any("heartbeat" in ln and "+1 since the backup" in ln for ln in lines)


def test_verify_catches_a_changed_file_and_a_backup_ahead_of_live(data_dir, tmp_path):
    out = tmp_path / "stage"
    backup.make(out, data_dir)
    (data_dir / "official.db").unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(data_dir / "official.db") + suffix).unlink(missing_ok=True)
    db.init_db(data_dir / "official.db").close()                  # live has fewer rows than the backup
    with pytest.raises(backup.BackupError, match="official.db.heartbeat: backup 1, live 0"):
        backup.verify(out, data_dir)
    with (out / "dev.db.gz").open("ab") as f:
        f.write(b"x")
    with pytest.raises(backup.BackupError, match="sha256"):
        backup.verify(out, data_dir)


def test_marker_is_what_the_summary_reads(tmp_path):
    m = tmp_path / "last.json"
    backup.write_marker("failed", "2026-12-14", "rclone copy", m)
    got = alerts.read_backup_marker(m)
    assert got["status"] == "failed" and got["step"] == "rclone copy" and got["date"] == "2026-12-14"
    assert backup.MARKER == alerts.BACKUP_MARKER


def test_backup_script_and_timer():
    sh = (ROOT / "deploy" / "backup.sh").read_text()
    assert "set -Eeuo pipefail" in sh and "trap fail ERR" in sh and "gdrive:alphalab-backups" in sh
    assert "runner.alerts notify" in sh and "KEEP_DAYS=30" in sh
    timer = (ROOT / "deploy" / "alphalab-backup.timer").read_text()
    assert "OnCalendar=*-*-* 02:30:00 UTC" in timer and "Persistent=true" in timer
    assert "alphalab-backup.timer" in (ROOT / "deploy" / "install.sh").read_text()
