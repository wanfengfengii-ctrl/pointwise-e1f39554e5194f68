import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from copies.volume import (DestructionRecord, VolumeBusy, VolumeLocation, VolumeReadError,
                           VolumeState)
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
        try:
            self._ensure_inventory_schema()
        except BaseException:
            self.db.close()
            raise

    def _ensure_inventory_schema(self) -> None:
        """Upgrade a version-1 volume store in place with the inventory guard fields."""
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(volumes)')}
        if not columns:
            return
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if {'inventory_guard', 'sealed'} <= columns and 'volume_destructions' in tables:
            return
        with transaction(self.db, write=True):
            if 'inventory_guard' not in columns:
                self.db.execute('ALTER TABLE volumes ADD COLUMN inventory_guard INTEGER NOT NULL DEFAULT 0')
            if 'sealed' not in columns:
                self.db.execute('ALTER TABLE volumes ADD COLUMN sealed INTEGER NOT NULL DEFAULT 0')
            self.db.execute('''
                CREATE TABLE IF NOT EXISTS volume_destructions (
                    volume_id TEXT PRIMARY KEY REFERENCES volumes,
                    operation_id TEXT NOT NULL,
                    inventory TEXT NOT NULL,
                    evidence TEXT NOT NULL
                )
            ''')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        self.db.close()

    def provision_volume(self, volume_id: str, slots: Mapping[str, OriginalRow | Mapping], *,
                         inventory_guard: bool = False) -> None:
        require_name(volume_id, 'volume_id')
        if type(inventory_guard) is not bool:
            raise ValueError('inventory_guard must be a boolean')
        entries = [(require_name(slot, 'slot'), parse_original_row(body)) for slot, body in slots.items()]
        with transaction(self.db, write=True):
            stored = self.db.execute('SELECT inventory_guard, sealed FROM volumes WHERE volume_id = ?',
                                     (volume_id,)).fetchone()
            if stored is not None:
                exists, guarded, sealed = True, bool(stored[0]), bool(stored[1])
                if inventory_guard and not guarded:
                    raise ValueError(f'cannot enable the inventory guard on an existing volume: {volume_id}')
                if self.destruction_id(volume_id) is not None:
                    raise ValueError(f'cannot provision a destroyed volume: {volume_id}')
                if sealed:
                    raise VolumeBusy(f'volume is under an inventory check: {volume_id}')
            else:
                exists, guarded, sealed = False, inventory_guard, False
            if not exists:
                self.db.execute('INSERT INTO volumes (volume_id, destroyed_by, inventory_guard, sealed) '
                                'VALUES (?, NULL, ?, 0)', (volume_id, int(guarded)))
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

    def is_inventory_guarded(self, volume_id: str) -> bool:
        stored = self.db.execute('SELECT inventory_guard FROM volumes WHERE volume_id = ?',
                                 (volume_id,)).fetchone()
        return stored is not None and bool(stored[0])

    def slot_entries(self, volume_id: str) -> tuple[tuple[str, OriginalRow | None], ...]:
        """Yield every physically present slot in name order, without catalog filtering.

        A slot whose stored body cannot be decoded yields ``None`` so the caller can
        treat it as unverifiable instead of losing the whole inventory.
        """
        stored = self.db.execute('SELECT destroyed_by FROM volumes WHERE volume_id = ?', (volume_id,)).fetchone()
        if stored is None:
            raise KeyError(volume_id)
        if stored[0] is not None:
            return ()
        rows = self.db.execute('SELECT slot, body FROM volume_slots WHERE volume_id = ? ORDER BY slot',
                               (volume_id,))
        entries = []
        for slot, body in rows:
            try:
                entries.append((slot, parse_original_row(json.loads(body))))
            except ValueError:
                entries.append((slot, None))
        return tuple(entries)

    def seal(self, volume_id: str) -> None:
        """Refuse appends while the caller holds an inventory check."""
        self.db.execute('UPDATE volumes SET sealed = 1 WHERE volume_id = ?', (volume_id,))

    def unseal(self, volume_id: str) -> None:
        self.db.execute('UPDATE volumes SET sealed = 0 WHERE volume_id = ?', (volume_id,))

    def destruction_record(self, volume_id: str) -> DestructionRecord | None:
        stored = self.db.execute('''
            SELECT operation_id, inventory, evidence FROM volume_destructions WHERE volume_id = ?
        ''', (volume_id,)).fetchone()
        if stored is None:
            return None
        operation_id, inventory, evidence = stored
        evidence_pairs = tuple((original_id, VolumeLocation(volume, slot))
                               for original_id, volume, slot in json.loads(evidence))
        inventory_pairs = tuple((slot, original_id) for slot, original_id in json.loads(inventory))
        return DestructionRecord(operation_id, inventory_pairs, evidence_pairs)

    def destroy(self, volume_id: str, operation_id: str, *,
                authorized_slots: frozenset[tuple[str, str]] | None = None,
                evidence: tuple[tuple[str, VolumeLocation], ...] = ()) -> VolumeState:
        require_name(operation_id, 'operation_id')
        with transaction(self.db, write=True):
            state = self.inspect(volume_id)
            if state is not VolumeState.INTACT:
                return state
            guarded = self.is_inventory_guarded(volume_id)
            entries = self.slot_entries(volume_id)
            if guarded and any(row is None for _, row in entries):
                raise ValueError(f'cannot clear an undecodable slot of {volume_id}')
            actual = tuple((slot, row.original_id) for slot, row in entries)
            if guarded and (authorized_slots is None or not set(actual) <= set(authorized_slots)):
                raise ValueError(f'destruction is not authorized for the actual slots of {volume_id}')
            other = self.db.execute('SELECT volume_id FROM volumes WHERE destroyed_by = ?', (operation_id,)).fetchone()
            if other is not None:
                raise ValueError(f'destruction operation already belongs to {other[0]}')
            self.db.execute('UPDATE volumes SET destroyed_by = ? WHERE volume_id = ?', (operation_id, volume_id))
            self.db.execute('DELETE FROM volume_slots WHERE volume_id = ?', (volume_id,))
            if guarded:
                encoded_inventory = json.dumps([[slot, original_id] for slot, original_id in actual],
                                               ensure_ascii=False)
                encoded_evidence = json.dumps([(original_id, location.volume_id, location.slot)
                                               for original_id, location in evidence], ensure_ascii=False)
                self.db.execute('''
                    INSERT INTO volume_destructions (volume_id, operation_id, inventory, evidence)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (volume_id) DO UPDATE SET operation_id = excluded.operation_id,
                        inventory = excluded.inventory, evidence = excluded.evidence
                ''', (volume_id, operation_id, encoded_inventory, encoded_evidence))
        return VolumeState.DESTROYED
