import sqlite3

from copies.catalog import CopyCatalog
from copies.volume import VolumePort, VolumeReplyLost, VolumeState
from retirement.coverage import assess_coverage
from retirement.journal import RetirementJournal, RetirementRequest, RetirementState
from storage import transaction


def begin_destruction(db: sqlite3.Connection, port: VolumePort, request_id: str) -> RetirementRequest:
    journal = RetirementJournal(db)
    with transaction(db, write=True):
        request = journal.require_request(request_id)
        if request.state is not RetirementState.READY:
            return request
        checked = assess_coverage(db, port, request.volume_id)
        if checked.missing:
            return journal.record_progress(request, RetirementState.WAITING,
                                           missing=checked.missing, evidence=checked.evidence)
        CopyCatalog(db).close_for_retirement(request.volume_id)
        return journal.record_progress(request, RetirementState.PENDING, missing=(),
                                       evidence=checked.evidence, operation_id=request.request_id)


def invoke_destruction(port: VolumePort, journal: RetirementJournal,
                       request: RetirementRequest) -> RetirementRequest:
    if request.state is not RetirementState.PENDING or request.operation_id is None:
        raise ValueError('destruction requires a committed intent')
    try:
        outcome = port.destroy(request.volume_id, request.operation_id)
    except VolumeReplyLost:
        return request
    if outcome is VolumeState.DESTROYED:
        return journal.record_outcome(request, outcome)
    return request
