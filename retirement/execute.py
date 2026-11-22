import sqlite3

from copies.catalog import CopyCatalog
from copies.volume import VolumePort, VolumeReplyLost, VolumeState
from retirement.assessment import check_volume
from retirement.inventory import destruction_scope
from retirement.journal import RetirementJournal, RetirementRequest, RetirementState
from storage import transaction


def settle_destruction(journal: RetirementJournal, port: VolumePort,
                       request: RetirementRequest) -> RetirementRequest:
    """Record the outcome using the volume's own record of what it actually cleared."""
    record = None
    reader = getattr(port, 'destruction_record', None)
    if reader is not None:
        record = reader(request.volume_id)
    if record is not None and record.operation_id == request.operation_id:
        return journal.record_outcome(request, VolumeState.DESTROYED,
                                      inventory=record.inventory, evidence=record.evidence)
    return journal.record_outcome(request, VolumeState.DESTROYED)


def begin_destruction(db: sqlite3.Connection, port: VolumePort, request_id: str) -> RetirementRequest:
    journal = RetirementJournal(db)
    with transaction(db, write=True):
        request = journal.require_request(request_id)
        if request.state is not RetirementState.READY:
            return request
        missing, unverified, evidence, inventory = check_volume(db, port, request.volume_id)
        if missing or unverified:
            return journal.record_progress(request, RetirementState.WAITING,
                                           missing=missing, evidence=evidence,
                                           unverified_slots=unverified, inventory=inventory)
        CopyCatalog(db).close_for_retirement(request.volume_id)
        return journal.record_progress(request, RetirementState.PENDING, missing=(),
                                       evidence=evidence, operation_id=request.request_id,
                                       unverified_slots=(), inventory=inventory)


def invoke_destruction(port: VolumePort, journal: RetirementJournal,
                       request: RetirementRequest) -> RetirementRequest:
    if request.state is not RetirementState.PENDING or request.operation_id is None:
        raise ValueError('destruction requires a committed intent')
    scope = destruction_scope(port, request.volume_id, request.inventory, request.evidence)
    try:
        outcome = port.destroy(request.volume_id, request.operation_id, **scope)
    except VolumeReplyLost:
        return request
    if outcome is VolumeState.DESTROYED:
        return settle_destruction(journal, port, request)
    return request
