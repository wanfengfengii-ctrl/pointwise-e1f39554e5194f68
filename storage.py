import sqlite3
from contextlib import contextmanager
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


def open_database(path: Path, application_id: int, *, create: bool = False) -> sqlite3.Connection:
    mode = 'rwc' if create else 'rw'
    db = None
    try:
        db = sqlite3.connect(f'{path.resolve().as_uri()}?mode={mode}', uri=True,
                             isolation_level=None, timeout=10)
        db.execute('PRAGMA foreign_keys = ON')
        if not create:
            stored_id = db.execute('PRAGMA application_id').fetchone()[0]
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if (stored_id, version) != (application_id, 1):
                raise WorkspaceError(f'{path.name} has an unrecognized or incomplete format')
    except (sqlite3.Error, WorkspaceError) as exc:
        if db is not None:
            db.close()
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError(f'cannot open {path.name}') from exc
    return db


def set_format(db: sqlite3.Connection, application_id: int) -> None:
    db.execute(f'PRAGMA application_id = {application_id}')
    db.execute('PRAGMA user_version = 1')


@contextmanager
def transaction(db: sqlite3.Connection, *, write: bool = False):
    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
