"""Daily database snapshots: consistent copies, newest three kept."""

import sqlite3
import time
from datetime import datetime, timedelta

from sqlmodel import Session

import app.backup as backup
from app.database import create_db_and_tables, get_engine
from app.models import Project


def _db(tmp_path):
    path = tmp_path / "trundlr.db"
    engine = get_engine(f"sqlite:///{path}")
    create_db_and_tables(engine)
    with Session(engine) as s:
        s.add(Project(name="Kept"))
        s.commit()
    return path


def test_database_path_only_for_sqlite_files(tmp_path):
    assert backup.database_path("sqlite:////app/data/trundlr.db").as_posix() == "/app/data/trundlr.db"
    assert backup.database_path("sqlite://") is None
    assert backup.database_path("sqlite:///:memory:") is None
    assert backup.database_path("postgresql://u@h/db") is None


def test_snapshot_is_a_readable_copy(tmp_path):
    path = _db(tmp_path)
    snap = backup.make_backup(path)
    assert snap.parent == tmp_path / "backups"
    names = sqlite3.connect(snap).execute("SELECT name FROM project").fetchall()
    assert names == [("Kept",)]
    assert not list(snap.parent.glob("*.partial"))


def test_only_the_newest_three_are_kept(tmp_path):
    path = _db(tmp_path)
    start = datetime(2026, 11, 1, 3, 0)
    for day in range(5):
        backup.make_backup(path, now=start + timedelta(days=day))
    kept = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert kept == ["trundlr-20261103-030000.db", "trundlr-20261104-030000.db",
                    "trundlr-20261105-030000.db"]


def test_due_when_none_exist_or_the_newest_is_a_day_old(tmp_path):
    path = _db(tmp_path)
    assert backup.backup_due(path, time.time())
    backup.make_backup(path)
    assert not backup.backup_due(path, time.time())
    assert backup.backup_due(path, time.time() + backup.BACKUP_INTERVAL_SECONDS)
