import sqlite3
from collections.abc import Callable

from copies.volume import VolumePort, VolumeState, VolumeStatusUnavailable
from intake.rows import require_name
from retirement.coverage import assess_coverage
from retirement.execute import begin_destruction
from retirement.journal import RetirementJournal, RetirementState
from retirement.journal import RetirementRequest as RetirementRequest
from retirement.reopen import reconcile_retirement
from storage import transaction


def retire(db: sqlite3.Connection, port: VolumePort, request_id: str, volume_id: str, *,
           on_ready: Callable[[RetirementRequest], None] | None = None) -> RetirementRequest:
    require_name(request_id, 'request_id')
    require_name(volume_id, 'volume_id')
    journal = RetirementJournal(db)
    with transaction(db, write=True):
        current = journal.read_request(request_id)
        if current is None:
            if not journal.knows_volume(volume_id):
                state = port.inspect(volume_id)
                if state is VolumeState.MISSING:
                    raise KeyError(volume_id)
                if state is VolumeState.UNKNOWN:
                    raise VolumeStatusUnavailable(volume_id)
                if state is VolumeState.DESTROYED:
                    raise ValueError(f'volume was destroyed without a recorded retirement: {volume_id}')
            current = journal.bind_request(request_id, volume_id)
        elif current.volume_id != volume_id:
            raise ValueError(f'retirement request already targets {current.volume_id}')
        if current.state in (RetirementState.WAITING, RetirementState.READY):
            checked = assess_coverage(db, port, volume_id)
            state = RetirementState.WAITING if checked.missing else RetirementState.READY
            current = journal.record_progress(current, state, missing=checked.missing, evidence=checked.evidence)

    if current.state is RetirementState.READY:
        if on_ready is not None:
            on_ready(current)
        current = begin_destruction(db, port, request_id)
    if current.state is RetirementState.PENDING:
        return reconcile_retirement(db, port, request_id)
    return current
