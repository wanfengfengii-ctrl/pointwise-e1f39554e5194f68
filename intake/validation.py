from collections.abc import Callable

from intake.manifest import BatchManifest
from intake.rows import OriginalRow


def check_existing_original(row: OriginalRow, stored: OriginalRow | None) -> None:
    if stored is not None and stored != row:
        raise ValueError(f'original already has different contents: {row.original_id}')


def validate_batch(manifest: BatchManifest, existing: BatchManifest | None,
                   find_original: Callable[[str], OriginalRow | None]) -> None:
    if existing is not None and existing != manifest:
        raise ValueError(f'batch already has a different manifest: {manifest.batch_id}')
    for entry in manifest.entries:
        row = entry.original
        check_existing_original(row, find_original(row.original_id))
