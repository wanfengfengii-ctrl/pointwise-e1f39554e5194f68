from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from intake.rows import OriginalRow, parse_original_row, require_name


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    position: int
    original: OriginalRow


@dataclass(frozen=True, slots=True)
class BatchManifest:
    batch_id: str
    entries: tuple[ManifestEntry, ...]


def build_manifest(batch_id: str, rows: Iterable[OriginalRow | Mapping]) -> BatchManifest:
    require_name(batch_id, 'batch_id')
    if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
        raise ValueError('batch rows must be an iterable of original rows')
    seen: dict[str, OriginalRow] = {}
    entries: list[ManifestEntry] = []
    for position, payload in enumerate(rows):
        row = parse_original_row(payload)
        previous = seen.get(row.original_id)
        if previous is not None:
            if previous != row:
                raise ValueError(f'conflicting original in input: {row.original_id}')
            continue
        seen[row.original_id] = row
        entries.append(ManifestEntry(position, row))
    return BatchManifest(batch_id, tuple(entries))
