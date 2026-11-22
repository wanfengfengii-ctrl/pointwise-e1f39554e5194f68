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


class VolumePort(Protocol):
    def read_slot(self, location: VolumeLocation) -> OriginalRow | Mapping:
        """Read the actual slot, or raise VolumeReadError if unreadable."""
        ...

    def inspect(self, volume_id: str) -> VolumeState:
        """INTACT confirms the volume remains and no earlier command can still take effect."""
        ...

    def destroy(self, volume_id: str, operation_id: str) -> VolumeState:
        """Apply the command idempotently. A lost reply can raise VolumeReplyLost."""
        ...
