"""Nightly backup files and the restore drill (deploy/backup.sh does the upload and retention).

    python -m runner.backup make DIR                 # every data/*.db -> DIR/<name>.db.gz + manifest.json
    python -m runner.backup marker ok|failed DATE [STEP]   # ~/.cache/alphalab-backup/last.json
    python -m runner.backup drill --to DIR [--date YYYY-MM-DD]   # download, gunzip, check, compare

make: for every data/*.db except the alphalab-backup-* copies, an SQLite online backup to a temp
file (consistent even while a tick writes), PRAGMA integrity_check on the copy (anything but 'ok'
fails), gzip, and a manifest with each file's sha256 and per-table row counts.

drill: rclone copy of gdrive:alphalab-backups/<date>/ (default: the newest folder) into DIR,
sha256 against the manifest, gunzip, integrity_check, and row counts per table against the
manifest (must be equal) and the live DB (live may be ahead, since the tables only grow; a
backup with more rows than live fails).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from data import db

REMOTE = "gdrive:alphalab-backups"
MARKER = Path.home() / ".cache" / "alphalab-backup" / "last.json"
SKIP_PREFIX = "alphalab-backup-"


class BackupError(Exception):
    pass


def sources(data_dir: Path = db.DATA_DIR) -> list[Path]:
    return sorted(p for p in data_dir.glob("*.db") if not p.name.startswith(SKIP_PREFIX))


def integrity(path: Path) -> str:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = [r[0] for r in c.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError as e:
        return f"{type(e).__name__}: {e}"
    finally:
        c.close()
    return "ok" if rows == ["ok"] else "; ".join(rows[:5])


def row_counts(path: Path) -> dict[str, int]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        c.close()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make(out: Path, data_dir: Path = db.DATA_DIR) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"made_at": db.utc_ts(), "files": {}}
    srcs = sources(data_dir)
    if not srcs:
        raise BackupError(f"no databases in {data_dir}")
    for src in srcs:
        tmp = out / f"{src.name}.tmp"
        tmp.unlink(missing_ok=True)
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
        d = sqlite3.connect(str(tmp))
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode = DELETE")     # one self-contained file (db.connect sets WAL again)
        finally:
            d.close()
            s.close()
        check = integrity(tmp)
        if check != "ok":
            raise BackupError(f"{src.name}: integrity_check on the copy: {check}")
        counts = row_counts(tmp)
        gz = out / f"{src.name}.gz"
        with tmp.open("rb") as f, gzip.open(gz, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1 << 20)
        for f in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
            f.unlink(missing_ok=True)
        manifest["files"][gz.name] = {"source": src.name, "sha256": sha256(gz), "bytes": gz.stat().st_size,
                                      "rows": counts}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def write_marker(status: str, day: str, step: str | None = None, path: Path = MARKER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    m = {"status": status, "date": day, "finished_at": db.utc_ts()}
    if step:
        m["step"] = step
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Restore drill
# ---------------------------------------------------------------------------

def _rclone(*args: str) -> str:
    r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise BackupError(f"rclone {args[0]} failed: {r.stderr.strip()[-500:]}")
    return r.stdout


def newest_remote(remote: str = REMOTE) -> str:
    days = sorted(x.strip().rstrip("/") for x in _rclone("lsf", "--dirs-only", remote).splitlines() if x.strip())
    days = [d for d in days if len(d) == 10 and d[4] == "-" and d[7] == "-"]
    if not days:
        raise BackupError(f"no dated folders in {remote}")
    return days[-1]


def verify(folder: Path, data_dir: Path = db.DATA_DIR) -> list[str]:
    """Check a downloaded backup folder; returns report lines, raises BackupError on any failure."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    lines, bad = [], []
    for gz_name, meta in sorted(manifest["files"].items()):
        gz = folder / gz_name
        if sha256(gz) != meta["sha256"]:
            bad.append(f"{gz_name}: sha256 differs from the manifest")
            continue
        restored = folder / meta["source"]
        with gzip.open(gz, "rb") as g, restored.open("wb") as f:
            shutil.copyfileobj(g, f, 1 << 20)
        check = integrity(restored)
        counts = row_counts(restored)
        live_path = data_dir / meta["source"]
        live = row_counts(live_path) if live_path.exists() else {}
        lines.append(f"{meta['source']}: integrity {check}; {sum(counts.values()):,} rows in {len(counts)} tables")
        if check != "ok":
            bad.append(f"{meta['source']}: integrity_check {check}")
        if counts != meta["rows"]:
            bad.append(f"{meta['source']}: row counts differ from the manifest")
        for t, n in counts.items():
            ln = live.get(t)
            note = "" if ln == n else (f"  (live {ln:,}, +{ln - n:,} since the backup)" if ln is not None and ln > n
                                       else f"  (live {ln}) BACKUP AHEAD OF LIVE")
            if ln is None or ln < n:
                bad.append(f"{meta['source']}.{t}: backup {n}, live {ln}")
            lines.append(f"    {t:<22} {n:>9,}{note}")
    if bad:
        raise BackupError("; ".join(bad))
    return lines


def drill(dest: Path, day: str | None = None, remote: str = REMOTE) -> list[str]:
    day = day or newest_remote(remote)
    folder = dest / day
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    _rclone("copy", f"{remote}/{day}", str(folder))
    return [f"restore drill of {remote}/{day} into {folder}"] + verify(folder)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Nightly backup files and the restore drill.")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("out", type=Path)
    k = sub.add_parser("marker")
    k.add_argument("status", choices=("ok", "failed"))
    k.add_argument("day")
    k.add_argument("step", nargs="?")
    d = sub.add_parser("drill")
    d.add_argument("--to", type=Path, required=True)
    d.add_argument("--date", type=date.fromisoformat, default=None)
    a = p.parse_args(argv)
    try:
        if a.cmd == "make":
            man = make(a.out)
            for name, meta in man["files"].items():
                print(f"{name}: {meta['bytes']:,} bytes, {sum(meta['rows'].values()):,} rows, integrity ok")
        elif a.cmd == "marker":
            write_marker(a.status, a.day, a.step)
        else:
            print("\n".join(drill(a.to, a.date.isoformat() if a.date else None)))
            print("drill: OK")
    except (BackupError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
