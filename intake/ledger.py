import json
import sqlite3
from dataclasses import asdict

from intake.manifest import BatchManifest, ManifestEntry
from intake.rows import OriginalRow, parse_original_row


class IntakeLedger:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create_schema(self) -> None:
        self.db.execute('CREATE TABLE batches (batch_id TEXT PRIMARY KEY)')
        self.db.execute('CREATE TABLE originals (original_id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('''
            CREATE TABLE batch_members (
                batch_id TEXT NOT NULL REFERENCES batches,
                position INTEGER NOT NULL CHECK (position >= 0),
                original_id TEXT NOT NULL REFERENCES originals,
                PRIMARY KEY (batch_id, position), UNIQUE (batch_id, original_id)
            )
        ''')

    def read_manifest(self, batch_id: str) -> BatchManifest | None:
        present = self.db.execute('SELECT 1 FROM batches WHERE batch_id = ?', (batch_id,)).fetchone()
        if present is None:
            return None
        entries = self.db.execute('''
            SELECT m.position, o.body FROM batch_members AS m
            JOIN originals AS o USING (original_id)
            WHERE m.batch_id = ? ORDER BY m.position
        ''', (batch_id,))
        return BatchManifest(batch_id, tuple(
            ManifestEntry(position, parse_original_row(json.loads(body))) for position, body in entries
        ))

    def find_original(self, original_id: str) -> OriginalRow | None:
        record = self.db.execute('SELECT body FROM originals WHERE original_id = ?', (original_id,)).fetchone()
        return None if record is None else parse_original_row(json.loads(record[0]))

    def write_manifest(self, manifest: BatchManifest) -> None:
        self.db.execute('INSERT INTO batches (batch_id) VALUES (?)', (manifest.batch_id,))
        for entry in manifest.entries:
            row = entry.original
            self.db.execute('''
                INSERT INTO originals (original_id, body) VALUES (?, ?)
                ON CONFLICT (original_id) DO NOTHING
            ''', (row.original_id, json.dumps(asdict(row), ensure_ascii=False)))
            self.db.execute('INSERT INTO batch_members VALUES (?, ?, ?)',
                            (manifest.batch_id, entry.position, row.original_id))
