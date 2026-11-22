from collections.abc import Mapping
from dataclasses import dataclass


def require_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonempty string')
    return value


@dataclass(frozen=True, slots=True)
class OriginalRow:
    original_id: str
    source: str
    reading: int | None


def parse_original_row(value: OriginalRow | Mapping) -> OriginalRow:
    if isinstance(value, OriginalRow):
        original_id, source, reading = value.original_id, value.source, value.reading
    elif isinstance(value, Mapping) and set(value) == {'original_id', 'source', 'reading'}:
        original_id, source, reading = value['original_id'], value['source'], value['reading']
    else:
        raise ValueError('an original row requires original_id, source and reading')
    if reading is not None and type(reading) is not int:
        raise ValueError('reading must be integer micrometres or missing')
    return OriginalRow(require_name(original_id, 'original_id'), require_name(source, 'source'), reading)
