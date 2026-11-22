import sqlite3
from dataclasses import dataclass

from copies.catalog import CopyCatalog
from copies.verify import CopyVerificationError, verify_copy
from copies.volume import VolumeLocation, VolumePort
from intake.ledger import IntakeLedger
from quality.eligibility import EligibilityReader


@dataclass(frozen=True, slots=True)
class CoverageAssessment:
    missing: tuple[str, ...]
    evidence: tuple[tuple[str, VolumeLocation], ...]


def assess_coverage(db: sqlite3.Connection, port: VolumePort, volume_id: str) -> CoverageAssessment:
    catalog = CopyCatalog(db)
    ledger = IntakeLedger(db)
    members = dict.fromkeys(record.original_id for record in catalog.list_for_volume(volume_id))
    eligible = EligibilityReader(db).eligible_ids(members)
    missing: list[str] = []
    evidence: list[tuple[str, VolumeLocation]] = []
    for original_id in eligible:
        expected = ledger.find_original(original_id)
        if expected is None:
            raise RuntimeError(f'copy catalog has no original for {original_id}')
        for record in catalog.list_for_original(original_id):
            candidate = record.location
            if candidate.volume_id == volume_id or not catalog.is_open_for_coverage(candidate.volume_id):
                continue
            try:
                verify_copy(port, candidate, expected)
            except CopyVerificationError:
                continue
            evidence.append((original_id, candidate))
            break
        else:
            missing.append(original_id)
    return CoverageAssessment(tuple(missing), tuple(evidence))
