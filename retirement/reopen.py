import sqlite3

from copies.volume import VolumePort, VolumeState
from retirement.coverage import assess_coverage
from retirement.execute import invoke_destruction
from retirement.journal import RetirementJournal, RetirementRequest, RetirementState
from storage import transaction


def reconcile_retirement(db: sqlite3.Connection, port: VolumePort, request_id: str) -> RetirementRequest:
    journal = RetirementJournal(db)
    # The intent has already committed. Serialize the status check and any dispatch
    # with other catalog writers while the physical store commits independently.
    with transaction(db, write=True):
        request = journal.require_request(request_id)
        if request.state is not RetirementState.PENDING:
            return request
        state = port.inspect(request.volume_id)
        if state is VolumeState.DESTROYED:
            return journal.record_outcome(request, state)
        if state is not VolumeState.INTACT:
            return request
        checked = assess_coverage(db, port, request.volume_id)
        request = journal.record_progress(request, RetirementState.PENDING,
                                          missing=checked.missing, evidence=checked.evidence,
                                          operation_id=request.operation_id)
        if checked.missing:
            return request
        return invoke_destruction(port, journal, request)
