from copies.volume import VolumeLocation, VolumePort, VolumeReadError
from intake.rows import OriginalRow, parse_original_row


class CopyVerificationError(ValueError):
    pass


def verify_copy(port: VolumePort, location: VolumeLocation, expected: OriginalRow) -> OriginalRow:
    try:
        actual = parse_original_row(port.read_slot(location))
    except (ValueError, VolumeReadError) as exc:
        raise CopyVerificationError(f'unreadable or invalid contents at {location}') from exc
    if actual != expected:
        raise CopyVerificationError(f'contents at {location} do not match {expected.original_id}')
    return actual
