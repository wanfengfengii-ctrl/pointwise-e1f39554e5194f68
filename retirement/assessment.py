"""Unified coverage assessment for ordinary and inventory-guarded volumes."""
import sqlite3

from copies.volume import VolumePort
from retirement.coverage import assess_coverage
from retirement.inventory import (assess_inventory, is_inventory_guarded, seal_volume,
                                  unseal_volume)


def check_volume(db: sqlite3.Connection, port: VolumePort, volume_id: str
                 ) -> tuple[tuple[str, ...], tuple[str, ...],
                            tuple[tuple[str, object], ...], tuple[tuple[str, str], ...]]:
    """Return ``(missing, unverified_slots, evidence, inventory)``.

    Ordinary volumes are assessed from registered copies alone; guarded volumes are
    assessed from every physically present slot, registered or not.
    """
    if is_inventory_guarded(port, volume_id):
        assessed = assess_inventory(db, port, volume_id)
        return assessed.missing, assessed.unverified_slots, assessed.evidence, assessed.inventory
    assessed = assess_coverage(db, port, volume_id)
    return assessed.missing, (), assessed.evidence, ()


def hold_inventory(port: VolumePort, volume_id: str) -> bool:
    """Seal a guarded volume so appends report busy; return whether it was held."""
    if is_inventory_guarded(port, volume_id):
        seal_volume(port, volume_id)
        return True
    return False


def release_inventory(port: VolumePort, volume_id: str) -> None:
    if is_inventory_guarded(port, volume_id):
        unseal_volume(port, volume_id)
