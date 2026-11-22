import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from copies.volume import VolumeLocation, VolumeReadError, VolumeState
from intake.rows import OriginalRow, parse_original_row, require_name
from storage import open_database, set_format, transaction


APPLICATION_ID = 0x42575631


class LocalVolumeStore:
    def __init__(self, path: Path, *, create: bool = False):
        self.db = open_database(Path(path), APPLICATION_ID, create=create)
        if create:
            try:
                with transaction(self.db, write=True):
                    self.db.execute('CREATE TABLE volumes (volume_id TEXT PRIMARY KEY, destroyed_by TEXT UNIQUE)')
                    self.db.execute('''
                        CREATE TABLE volume_slots (
                            volume_id TEXT NOT NULL REFERENCES volumes,
                            slot TEXT NOT NULL, body TEXT NOT NULL,
                            PRIMARY KEY (volume_id, slot)
                        )
                    ''')
                    set_format(self.db, APPLICATION_ID)
            except BaseException:
                self.db.close()
                raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        self.db.close()

    def provision_volume(self, volume_id: str, slots: Mapping[str, OriginalRow | Mapping]) -> None:
        require_name(volume_id, 'volume_id')
        entries = [(require_name(slot, 'slot'), parse_original_row(body)) for slot, body in slots.items()]
        with transaction(self.db, write=True):
            self.db.execute('INSERT INTO volumes VALUES (?, NULL) ON CONFLICT DO NOTHING', (volume_id,))
            if self.destruction_id(volume_id) is not None:
                raise ValueError(f'cannot provision a destroyed volume: {volume_id}')
            for slot, row in entries:
                previous = self.db.execute('''
                    SELECT body FROM volume_slots WHERE volume_id = ? AND slot = ?
                ''', (volume_id, slot)).fetchone()
                if previous is None:
                    self.db.execute('INSERT INTO volume_slots VALUES (?, ?, ?)',
                                    (volume_id, slot, json.dumps(asdict(row), ensure_ascii=False)))
                elif parse_original_row(json.loads(previous[0])) != row:
                    raise ValueError(f'physical contents already fixed at {volume_id}/{slot}')

    def read_slot(self, location: VolumeLocation) -> OriginalRow:
        stored = self.db.execute('''
            SELECT s.body FROM volume_slots AS s JOIN volumes AS v USING (volume_id)
            WHERE s.volume_id = ? AND s.slot = ? AND v.destroyed_by IS NULL
        ''', (location.volume_id, location.slot)).fetchone()
        if stored is None:
            raise VolumeReadError(f'no readable slot at {location}')
        try:
            return parse_original_row(json.loads(stored[0]))
        except ValueError as exc:
            raise VolumeReadError(f'cannot decode slot at {location}') from exc

    def inspect(self, volume_id: str) -> VolumeState:
        stored = self.db.execute('SELECT destroyed_by FROM volumes WHERE volume_id = ?', (volume_id,)).fetchone()
        if stored is None:
            return VolumeState.MISSING
        return VolumeState.INTACT if stored[0] is None else VolumeState.DESTROYED

    def destruction_id(self, volume_id: str) -> str | None:
        stored = self.db.execute('SELECT destroyed_by FROM volumes WHERE volume_id = ?', (volume_id,)).fetchone()
        if stored is None:
            raise KeyError(volume_id)
        return stored[0]

    def destroy(self, volume_id: str, operation_id: str) -> VolumeState:
        require_name(operation_id, 'operation_id')
        with transaction(self.db, write=True):
            state = self.inspect(volume_id)
            if state is not VolumeState.INTACT:
                return state
            other = self.db.execute('SELECT volume_id FROM volumes WHERE destroyed_by = ?', (operation_id,)).fetchone()
            if other is not None:
                raise ValueError(f'destruction operation already belongs to {other[0]}')
            self.db.execute('UPDATE volumes SET destroyed_by = ? WHERE volume_id = ?', (operation_id, volume_id))
            self.db.execute('DELETE FROM volume_slots WHERE volume_id = ?', (volume_id,))
        return VolumeState.DESTROYED
