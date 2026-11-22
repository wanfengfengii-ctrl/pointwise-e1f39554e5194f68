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


class RetirementJournal:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create_schema(self) -> None:
        self.db.execute('''
            CREATE TABLE retirement_progress (
                volume_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('waiting', 'ready', 'pending', 'retired')),
                missing TEXT NOT NULL, evidence TEXT NOT NULL,
                operation_id TEXT UNIQUE,
                outcome TEXT CHECK (outcome = 'destroyed'),
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
            SELECT r.volume_id, p.state, p.missing, p.evidence, p.operation_id, p.outcome
            FROM retirement_requests AS r JOIN retirement_progress AS p USING (volume_id)
            WHERE r.request_id = ?
        ''', (request_id,)).fetchone()
        if stored is None:
            return None
        volume_id, state, missing, evidence, operation_id, outcome = stored
        locations = tuple((original_id, VolumeLocation(volume, slot))
                          for original_id, volume, slot in json.loads(evidence))
        return RetirementRequest(request_id, volume_id, RetirementState(state), tuple(json.loads(missing)),
                                 locations, operation_id, None if outcome is None else VolumeState(outcome))

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
                        operation_id: str | None = None) -> RetirementRequest:
        encoded = json.dumps([(original_id, location.volume_id, location.slot)
                              for original_id, location in evidence], ensure_ascii=False)
        self.db.execute('''
            UPDATE retirement_progress SET state = ?, missing = ?, evidence = ?, operation_id = ?
            WHERE volume_id = ?
        ''', (state.value, json.dumps(missing, ensure_ascii=False), encoded, operation_id, request.volume_id))
        return self.require_request(request.request_id)

    def record_outcome(self, request: RetirementRequest, outcome: VolumeState) -> RetirementRequest:
        if request.state is not RetirementState.PENDING or outcome is not VolumeState.DESTROYED:
            raise ValueError('a pending request and confirmed destruction are required')
        self.db.execute('''
            UPDATE retirement_progress SET state = 'retired', outcome = ?, missing = '[]'
            WHERE volume_id = ? AND state = 'pending'
        ''', (outcome.value, request.volume_id))
        return self.require_request(request.request_id)
