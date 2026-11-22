"""Physical inventory checks for volumes carried under the inventory guard."""
import sqlite3
from dataclasses import dataclass

from copies.catalog import CopyCatalog
from copies.verify import CopyVerificationError, verify_copy
from copies.volume import VolumeLocation, VolumePort
from intake.ledger import IntakeLedger
from intake.rows import OriginalRow
from quality.eligibility import EligibilityReader


@dataclass(frozen=True, slots=True)
class InventoryAssessment:
    missing: tuple[str, ...]
    unverified_slots: tuple[str, ...]
    evidence: tuple[tuple[str, VolumeLocation], ...]
    inventory: tuple[tuple[str, str], ...]


def is_inventory_guarded(port: VolumePort, volume_id: str) -> bool:
    check = getattr(port, 'is_inventory_guarded', None)
    if check is None:
        return False
    try:
        return bool(check(volume_id))
    except KeyError:
        return False


def _actual_slots(port: VolumePort, volume_id: str) -> tuple[tuple[str, OriginalRow | None], ...]:
    reader = getattr(port, 'slot_entries', None)
    if reader is None:
        return ()
    return tuple(reader(volume_id))


def assess_inventory(db: sqlite3.Connection, port: VolumePort, volume_id: str) -> InventoryAssessment:
    """Check every physically present slot, registered or not, against the intake ledger."""
    catalog = CopyCatalog(db)
    ledger = IntakeLedger(db)
    verified: list[tuple[str, OriginalRow]] = []
    unverified: list[str] = []
    for slot, body in _actual_slots(port, volume_id):
        if body is None:
            unverified.append(slot)
            continue
        expected = ledger.find_original(body.original_id)
        if expected is None or expected != body:
            unverified.append(slot)
            continue
        verified.append((slot, body))

    identities: dict[str, None] = dict.fromkeys(body.original_id for _, body in verified)
    eligible = EligibilityReader(db).eligible_ids(identities)
    missing: list[str] = []
    evidence: list[tuple[str, VolumeLocation]] = []
    for original_id in eligible:
        expected = ledger.find_original(original_id)
        if expected is None:
            raise RuntimeError(f'volume contents reference an unknown original: {original_id}')
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
    inventory = tuple((slot, body.original_id) for slot, body in verified)
    return InventoryAssessment(tuple(missing), tuple(unverified), tuple(evidence), inventory)


def destruction_scope(port: VolumePort, volume_id: str,
                      inventory: tuple[tuple[str, str], ...],
                      evidence: tuple[tuple[str, VolumeLocation], ...]) -> dict:
    """Authorize exactly the inventory that was assessed for this destruction.

    A guarded volume clears only slots covered by this set; anything appended after
    the assessment makes the physical store refuse the command.
    """
    if not is_inventory_guarded(port, volume_id):
        return {}
    return {'authorized_slots': frozenset(inventory), 'evidence': evidence}


def seal_volume(port: VolumePort, volume_id: str) -> None:
    seal = getattr(port, 'seal', None)
    if seal is not None:
        seal(volume_id)


def unseal_volume(port: VolumePort, volume_id: str) -> None:
    unseal = getattr(port, 'unseal', None)
    if unseal is not None:
        unseal(volume_id)
