import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

from copies.volume import VolumeLocation, VolumeState


class RetirementState(str, Enum):
    WAITING = 'waiting'
    READY = 'ready'
    PENDING = 'pending'
    RETIRED = 'retired'


@dataclass(frozen=True, slots=True)
class RetirementRequest:
    request_id: str
    volume_id: str
    state: RetirementState
    missing: tuple[str, ...]
    evidence: tuple[tuple[str, VolumeLocation], ...]
    operation_id: str | None
    outcome: VolumeState | None
    unverified_slots: tuple[str, ...] = ()
    inventory: tuple[tuple[str, str], ...] = ()


class RetirementJournal:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def upgrade_schema(self) -> None:
        """Add the inventory-guard columns to an earlier version-1 catalog."""
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(retirement_progress)')}
        if not columns or {'unverified_slots', 'inventory'} <= columns:
            return
        from storage import transaction
        with transaction(self.db, write=True):
            if 'unverified_slots' not in columns:
                self.db.execute("ALTER TABLE retirement_progress ADD COLUMN unverified_slots TEXT NOT NULL DEFAULT '[]'")
            if 'inventory' not in columns:
                self.db.execute("ALTER TABLE retirement_progress ADD COLUMN inventory TEXT NOT NULL DEFAULT '[]'")

    def create_schema(self) -> None:
        self.db.execute('''
            CREATE TABLE retirement_progress (
                volume_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('waiting', 'ready', 'pending', 'retired')),
                missing TEXT NOT NULL, evidence TEXT NOT NULL,
                operation_id TEXT UNIQUE,
                outcome TEXT CHECK (outcome = 'destroyed'),
                unverified_slots TEXT NOT NULL DEFAULT '[]',
                inventory TEXT NOT NULL DEFAULT '[]',
                CHECK ((operation_id IS NOT NULL) = (state IN ('pending', 'retired'))),
                CHECK ((outcome IS NOT NULL) = (state = 'retired'))
            )
        ''')
        self.db.execute('''
            CREATE TABLE retirement_requests (
                request_id TEXT PRIMARY KEY,
                volume_id TEXT NOT NULL REFERENCES retirement_progress
            )
        ''')

    def knows_volume(self, volume_id: str) -> bool:
        return self.db.execute('SELECT 1 FROM retirement_progress WHERE volume_id = ?',
                               (volume_id,)).fetchone() is not None

    def read_request(self, request_id: str) -> RetirementRequest | None:
        stored = self.db.execute('''
            SELECT r.volume_id, p.state, p.missing, p.evidence, p.operation_id, p.outcome,
                   p.unverified_slots, p.inventory
            FROM retirement_requests AS r JOIN retirement_progress AS p USING (volume_id)
            WHERE r.request_id = ?
        ''', (request_id,)).fetchone()
        if stored is None:
            return None
        volume_id, state, missing, evidence, operation_id, outcome, unverified, inventory = stored
        locations = tuple((original_id, VolumeLocation(volume, slot))
                          for original_id, volume, slot in json.loads(evidence))
        inventory_pairs = tuple((slot, original_id) for slot, original_id in json.loads(inventory))
        return RetirementRequest(request_id, volume_id, RetirementState(state), tuple(json.loads(missing)),
                                 locations, operation_id, None if outcome is None else VolumeState(outcome),
                                 tuple(json.loads(unverified)), inventory_pairs)

    def require_request(self, request_id: str) -> RetirementRequest:
        request = self.read_request(request_id)
        if request is None:
            raise KeyError(request_id)
        return request

    def bind_request(self, request_id: str, volume_id: str) -> RetirementRequest:
        self.db.execute('''
            INSERT INTO retirement_progress (volume_id, state, missing, evidence)
            VALUES (?, 'waiting', '[]', '[]') ON CONFLICT (volume_id) DO NOTHING
        ''', (volume_id,))
        self.db.execute('INSERT INTO retirement_requests VALUES (?, ?)', (request_id, volume_id))
        return self.require_request(request_id)

    def record_progress(self, request: RetirementRequest, state: RetirementState, *,
                        missing: tuple[str, ...], evidence: tuple[tuple[str, VolumeLocation], ...],
                        operation_id: str | None = None,
                        unverified_slots: tuple[str, ...] = (),
                        inventory: tuple[tuple[str, str], ...] = ()) -> RetirementRequest:
        encoded = json.dumps([(original_id, location.volume_id, location.slot)
                              for original_id, location in evidence], ensure_ascii=False)
        self.db.execute('''
            UPDATE retirement_progress SET state = ?, missing = ?, evidence = ?, operation_id = ?,
                unverified_slots = ?, inventory = ?
            WHERE volume_id = ?
        ''', (state.value, json.dumps(missing, ensure_ascii=False), encoded, operation_id,
              json.dumps(unverified_slots, ensure_ascii=False),
              json.dumps([list(pair) for pair in inventory], ensure_ascii=False), request.volume_id))
        return self.require_request(request.request_id)

    def record_outcome(self, request: RetirementRequest, outcome: VolumeState, *,
                       inventory: tuple[tuple[str, str], ...] | None = None,
                       evidence: tuple[tuple[str, VolumeLocation], ...] | None = None) -> RetirementRequest:
        if request.state is not RetirementState.PENDING or outcome is not VolumeState.DESTROYED:
            raise ValueError('a pending request and confirmed destruction are required')
        actual_inventory = request.inventory if inventory is None else inventory
        actual_evidence = request.evidence if evidence is None else evidence
        self.db.execute('''
            UPDATE retirement_progress SET state = 'retired', outcome = ?, missing = '[]',
                unverified_slots = '[]', inventory = ?, evidence = ?
            WHERE volume_id = ? AND state = 'pending'
        ''', (outcome.value,
              json.dumps([list(pair) for pair in actual_inventory], ensure_ascii=False),
              json.dumps([(original_id, location.volume_id, location.slot)
                          for original_id, location in actual_evidence], ensure_ascii=False),
              request.volume_id))
        return self.require_request(request.request_id)
