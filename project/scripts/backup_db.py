"""
Consistent online backup of the SQLite database using the SQLite backup API
(safe while the app is running, unlike copying the file).

  python scripts/backup_db.py --dest backups/ --keep 14
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import config_by_name  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", default="backups")
    parser.add_argument("--keep", type=int, default=14, help="how many backups to retain")
    args = parser.parse_args()

    cfg = config_by_name[os.environ.get("FLASK_ENV", "development")]
    source = sqlite3.connect(cfg.DATABASE_PATH)
    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"customer_support-{stamp}.db"
    target_conn = sqlite3.connect(target)
    try:
        source.backup(target_conn)
        result = target_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"Backup failed integrity check: {result}")
    finally:
        target_conn.close()
        source.close()

    backups = sorted(dest_dir.glob("customer_support-*.db"))
    for old in backups[:-args.keep] if args.keep > 0 else []:
        old.unlink()
    print(f"Backup written and verified: {target}")


if __name__ == "__main__":
    main()
