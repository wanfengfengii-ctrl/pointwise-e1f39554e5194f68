import sqlite3
from collections.abc import Iterable, Mapping

from intake.ledger import IntakeLedger
from intake.manifest import BatchManifest, build_manifest
from intake.rows import OriginalRow
from intake.validation import validate_batch
from storage import transaction


def load(db: sqlite3.Connection, batch_id: str,
         rows: Iterable[OriginalRow | Mapping]) -> BatchManifest:
    manifest = build_manifest(batch_id, rows)
    ledger = IntakeLedger(db)
    with transaction(db, write=True):
        existing = ledger.read_manifest(batch_id)
        validate_batch(manifest, existing, ledger.find_original)
        if existing is not None:
            return existing
        ledger.write_manifest(manifest)
    return manifest
