from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_config


def create_backup() -> Path:
    config = load_config()
    backup_dir = Path(os.getenv("PK_BACKUP_DIR", config.data_dir / "backups")).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        backup_dir.chmod(0o700)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = backup_dir / f"projektkontor-{stamp}"
    work.mkdir(mode=0o700)

    source = sqlite3.connect(config.db_path)
    target = sqlite3.connect(work / "projektkontor.sqlite3")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    if config.upload_dir.exists():
        shutil.copytree(config.upload_dir, work / "uploads")
    if config.report_dir.exists():
        shutil.copytree(config.report_dir, work / "reports")

    archive = backup_dir / f"projektkontor-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(work, arcname="projektkontor")
    try:
        archive.chmod(0o600)
    except OSError:
        pass
    shutil.rmtree(work)

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(os.getenv("PK_BACKUP_RETENTION_DAYS", "14")))
    for candidate in backup_dir.glob("projektkontor-*.tar.gz"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            candidate.unlink()
    return archive


def main() -> None:
    print(create_backup())


if __name__ == "__main__":
    main()
