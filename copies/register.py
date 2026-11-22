import sqlite3
from collections.abc import Mapping

from copies.catalog import CopyCatalog, CopyRecord
from copies.verify import CopyVerificationError, verify_copy
from copies.volume import VolumeLocation, VolumePort
from intake.ledger import IntakeLedger
from intake.rows import OriginalRow, parse_original_row, require_name
from storage import transaction


def register(db: sqlite3.Connection, port: VolumePort, original_id: str,
             location: VolumeLocation, candidate: OriginalRow | Mapping) -> CopyRecord:
    require_name(original_id, 'original_id')
    proposed = parse_original_row(candidate)
    if not isinstance(location, VolumeLocation):
        raise ValueError('location must be a VolumeLocation')
    catalog = CopyCatalog(db)
    with transaction(db, write=True):
        expected = IntakeLedger(db).find_original(original_id)
        if expected is None:
            raise KeyError(original_id)
        if expected != proposed:
            raise CopyVerificationError(f'candidate does not match {original_id}')
        if not catalog.is_open_for_coverage(location.volume_id):
            raise ValueError(f'volume is closed for new registrations: {location.volume_id}')
        verify_copy(port, location, expected)
        return catalog.record_verified(original_id, location)
