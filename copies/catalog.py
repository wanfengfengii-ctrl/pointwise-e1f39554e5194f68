import sqlite3
from dataclasses import dataclass

from copies.volume import VolumeLocation


@dataclass(frozen=True, slots=True)
class CopyRecord:
    original_id: str
    location: VolumeLocation


class CopyCatalog:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create_schema(self) -> None:
        self.db.execute('''
            CREATE TABLE verified_copies (
                volume_id TEXT NOT NULL, slot TEXT NOT NULL,
                original_id TEXT NOT NULL REFERENCES originals,
                PRIMARY KEY (volume_id, slot)
            )
        ''')
        self.db.execute('CREATE INDEX copies_by_original ON verified_copies (original_id, volume_id, slot)')
        self.db.execute('CREATE TABLE closed_volumes (volume_id TEXT PRIMARY KEY)')

    def record_verified(self, original_id: str, location: VolumeLocation) -> CopyRecord:
        if not self.is_open_for_coverage(location.volume_id):
            raise ValueError(f'volume is closed for new registrations: {location.volume_id}')
        coordinates = (location.volume_id, location.slot)
        existing = self.db.execute('''
            SELECT original_id FROM verified_copies WHERE volume_id = ? AND slot = ?
        ''', coordinates).fetchone()
        if existing is None:
            self.db.execute('INSERT INTO verified_copies VALUES (?, ?, ?)', (*coordinates, original_id))
        elif existing[0] != original_id:
            raise ValueError(f'volume slot already belongs to {existing[0]}')
        return CopyRecord(original_id, location)

    def list_for_original(self, original_id: str) -> tuple[CopyRecord, ...]:
        records = self.db.execute('''
            SELECT volume_id, slot FROM verified_copies
            WHERE original_id = ? ORDER BY volume_id, slot
        ''', (original_id,))
        return tuple(CopyRecord(original_id, VolumeLocation(volume, slot)) for volume, slot in records)

    def list_for_volume(self, volume_id: str) -> tuple[CopyRecord, ...]:
        records = self.db.execute('''
            SELECT original_id, slot FROM verified_copies
            WHERE volume_id = ? ORDER BY original_id, slot
        ''', (volume_id,))
        return tuple(CopyRecord(original_id, VolumeLocation(volume_id, slot)) for original_id, slot in records)

    def close_for_retirement(self, volume_id: str) -> None:
        self.db.execute('INSERT INTO closed_volumes VALUES (?) ON CONFLICT DO NOTHING', (volume_id,))

    def is_open_for_coverage(self, volume_id: str) -> bool:
        row = self.db.execute('SELECT 1 FROM closed_volumes WHERE volume_id = ?', (volume_id,)).fetchone()
        return row is None
