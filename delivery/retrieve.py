import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable

from copies.catalog import CopyCatalog, CopyRecord
from copies.verify import CopyVerificationError, verify_copy
from copies.volume import VolumeLocation, VolumePort
from delivery.issue import issue_receipt
from delivery.receipt_store import Receipt, ReceiptLine
from intake.ledger import IntakeLedger
from intake.manifest import BatchManifest, ManifestEntry
from intake.rows import OriginalRow, require_name
from quality.eligibility import EligibilityReader
from storage import transaction


class DeliveryUnavailable(RuntimeError):
    def __init__(self, original_ids: tuple[str, ...]):
        self.original_ids = original_ids
        super().__init__('cannot verify effective originals: ' + ', '.join(original_ids))


def _eligible_entries(manifest: BatchManifest, eligibility: EligibilityReader) -> tuple[ManifestEntry, ...]:
    eligible = set(eligibility.eligible_ids(entry.original.original_id for entry in manifest.entries))
    return tuple(entry for entry in manifest.entries if entry.original.original_id in eligible)


def _read_original(port: VolumePort, entry: ManifestEntry, candidates: Iterable[CopyRecord],
                   attempted: set[VolumeLocation]) -> OriginalRow | None:
    for record in candidates:
        if record.location in attempted:
            continue
        attempted.add(record.location)
        try:
            return verify_copy(port, record.location, entry.original)
        except CopyVerificationError:
            continue
    return None


def _make_line(entry: ManifestEntry, body: OriginalRow) -> ReceiptLine:
    return ReceiptLine(entry.position, body.original_id, body.source, body.reading)


def get(db: sqlite3.Connection, port: VolumePort, batch_id: str, *,
        on_open: Callable[[BatchManifest], None] | None = None,
        before_read: Callable[[ManifestEntry], None] | None = None,
        after_fetch: Callable[[tuple[OriginalRow, ...]], None] | None = None) -> Receipt:
    require_name(batch_id, 'batch_id')
    catalog = CopyCatalog(db)
    eligibility = EligibilityReader(db)
    with transaction(db):
        manifest = IntakeLedger(db).read_manifest(batch_id)
        if manifest is None:
            raise KeyError(batch_id)
        plan = [(entry, catalog.list_for_original(entry.original.original_id))
                for entry in _eligible_entries(manifest, eligibility)]

    if on_open is not None:
        on_open(manifest)
    verified: dict[str, OriginalRow] = {}
    attempted: dict[str, set[VolumeLocation]] = defaultdict(set)
    for entry, candidates in plan:
        if before_read is not None:
            before_read(entry)
        original_id = entry.original.original_id
        body = _read_original(port, entry, candidates, attempted[original_id])
        if body is not None:
            verified[original_id] = body
    if after_fetch is not None:
        after_fetch(tuple(verified.values()))

    while True:
        with transaction(db, write=True):
            eligible = _eligible_entries(manifest, eligibility)
            missing = [entry for entry in eligible if entry.original.original_id not in verified]
            if not missing:
                lines = tuple(_make_line(entry, verified[entry.original.original_id]) for entry in eligible)
                return issue_receipt(db, batch_id, lines)
            supplement = []
            for entry in missing:
                original_id = entry.original.original_id
                candidates = tuple(record for record in catalog.list_for_original(original_id)
                                   if record.location not in attempted[original_id])
                if candidates:
                    supplement.append((entry, candidates))
            if not supplement:
                raise DeliveryUnavailable(tuple(entry.original.original_id for entry in missing))

        for entry, candidates in supplement:
            original_id = entry.original.original_id
            body = _read_original(port, entry, candidates, attempted[original_id])
            if body is not None:
                verified[original_id] = body
