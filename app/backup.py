"""Daily database snapshots, kept beside the database in backups/.

Uses SQLite's online backup API, so a snapshot is consistent even while the app
is writing (WAL mode). Only the newest BACKUP_KEEP snapshots are kept. They
live in the same volume as the database: they guard against a bad edit or a
corrupted file, not against losing the disk.
"""

import asyncio
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

BACKUP_KEEP = 3
BACKUP_INTERVAL_SECONDS = 24 * 3600
# The loop waits before its first check so short-lived app starts (tests, a
# quick restart) don't each write a snapshot, then checks hourly.
FIRST_CHECK_DELAY_SECONDS = 10 * 60
CHECK_INTERVAL_SECONDS = 3600


def database_path(database_url: str) -> Optional[Path]:
    """The SQLite file behind a database URL, or None if there isn't one."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def backup_dir(db_path: Path) -> Path:
    return db_path.parent / "backups"


def _snapshots(directory: Path) -> list[Path]:
    # Timestamped names sort oldest → newest.
    return sorted(directory.glob("trundlr-*.db"))


def backup_due(db_path: Path, now: float) -> bool:
    """True if no snapshot exists or the newest is a day old (`now` is epoch seconds)."""
    snaps = _snapshots(backup_dir(db_path))
    return not snaps or now - snaps[-1].stat().st_mtime >= BACKUP_INTERVAL_SECONDS


def make_backup(db_path: Path, now: Optional[datetime] = None) -> Path:
    """Snapshot the database and prune all but the newest BACKUP_KEEP."""
    directory = backup_dir(db_path)
    directory.mkdir(exist_ok=True)
    target = directory / f"trundlr-{(now or datetime.now()):%Y%m%d-%H%M%S}.db"
    partial = target.with_suffix(".partial")
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(partial)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    partial.rename(target)  # never leave a half-written file under a snapshot name
    for old in _snapshots(directory)[:-BACKUP_KEEP]:
        old.unlink()
    return target


async def backup_loop(database_url: str) -> None:
    """Background task started by the app lifespan."""
    db_path = database_path(database_url)
    if db_path is None:
        return
    await asyncio.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            if backup_due(db_path, time.time()):
                target = await asyncio.to_thread(make_backup, db_path)
                logger.info("database backup written: %s", target)
        except Exception:
            logger.exception("database backup failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
