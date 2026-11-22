import sqlite3

from copies.volume import VolumePort, VolumeState
from retirement.assessment import check_volume, hold_inventory, release_inventory
from retirement.execute import settle_destruction, invoke_destruction
from retirement.journal import RetirementJournal, RetirementRequest, RetirementState
from storage import transaction


def reconcile_retirement(db: sqlite3.Connection, port: VolumePort, request_id: str) -> RetirementRequest:
    journal = RetirementJournal(db)
    # The intent has already committed. Serialize the status check and any dispatch
    # with other catalog writers while the physical store commits independently.
    held = False
    try:
        with transaction(db, write=True):
            request = journal.require_request(request_id)
            if request.state is not RetirementState.PENDING:
                return request
            state = port.inspect(request.volume_id)
            if state is VolumeState.DESTROYED:
                # The volume committed, but the catalog has not settled. Recover the
                # actual evidence and inventory from the volume's own record when present.
                record = None
                reader = getattr(port, 'destruction_record', None)
                if reader is not None:
                    record = reader(request.volume_id)
                if record is not None and record.operation_id == request.operation_id:
                    return journal.record_outcome(request, state, inventory=record.inventory,
                                                  evidence=record.evidence)
                return journal.record_outcome(request, state)
            if state is not VolumeState.INTACT:
                return request
            held = hold_inventory(port, request.volume_id)
            missing, unverified, evidence, inventory = check_volume(db, port, request.volume_id)
            if missing or unverified:
                # The volume is confirmed intact, so the earlier command can no longer take
                # effect. The same request may still be settled after the gaps are supplied.
                return journal.record_progress(request, RetirementState.WAITING,
                                               missing=missing, evidence=evidence,
                                               operation_id=None,
                                               unverified_slots=unverified, inventory=inventory)
            request = journal.record_progress(request, RetirementState.PENDING,
                                              missing=(), evidence=evidence,
                                              operation_id=request.operation_id,
                                              unverified_slots=(), inventory=inventory)
        return invoke_destruction(port, journal, request)
    finally:
        if held:
            release_inventory(port, request.volume_id)
