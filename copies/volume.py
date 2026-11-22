from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from intake.rows import OriginalRow, require_name


@dataclass(frozen=True, slots=True)
class VolumeLocation:
    volume_id: str
    slot: str

    def __post_init__(self):
        require_name(self.volume_id, 'volume_id')
        require_name(self.slot, 'slot')


class VolumeState(str, Enum):
    INTACT = 'intact'
    DESTROYED = 'destroyed'
    UNKNOWN = 'unknown'
    MISSING = 'missing'


class VolumeReadError(OSError):
    pass


class VolumeReplyLost(OSError):
    pass


class VolumeStatusUnavailable(RuntimeError):
    pass


class VolumeBusy(RuntimeError):
    """The volume is under an inventory check; the append may be retried later."""


@dataclass(frozen=True, slots=True)
class DestructionRecord:
    operation_id: str
    inventory: tuple[tuple[str, str], ...]
    evidence: tuple[tuple[str, VolumeLocation], ...]


class VolumePort(Protocol):
    def read_slot(self, location: VolumeLocation) -> OriginalRow | Mapping:
        """Read the actual slot, or raise VolumeReadError if unreadable."""
        ...

    def inspect(self, volume_id: str) -> VolumeState:
        """INTACT confirms the volume remains and no earlier command can still take effect."""
        ...

    def destroy(self, volume_id: str, operation_id: str, *,
                authorized_slots: frozenset[tuple[str, str]] | None = None,
                evidence: tuple[tuple[str, VolumeLocation], ...] = ()) -> VolumeState:
        """Apply the command idempotently. A lost reply can raise VolumeReplyLost.

        ``authorized_slots`` is the set of ``(slot, original_id)`` pairs the caller may
        physically clear. An inventory-guarded volume refuses any call that does not
        cover its actual contents, leaving the volume intact.
        """
        ...
