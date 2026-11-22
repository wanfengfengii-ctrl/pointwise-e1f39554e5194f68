import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from archive import Archive, initialize
from copies.physical_store import LocalVolumeStore
from copies.volume import VolumeLocation, VolumeReplyLost, VolumeState
from intake.ledger import IntakeLedger
from intake.rows import OriginalRow


class InterruptedVolumeStore(LocalVolumeStore):
    """Use the real volume database with deterministic interruptions at the port."""

    lose_after_commit = False
    interrupt_before_dispatch = False
    unavailable_volume = None

    def inspect(self, volume_id):
        if self.unavailable_volume == volume_id:
            return VolumeState.UNKNOWN
        return super().inspect(volume_id)

    def destroy(self, volume_id, operation_id):
        if self.interrupt_before_dispatch:
            self.interrupt_before_dispatch = False
            raise VolumeReplyLost('command did not reach the volume')
        state = super().destroy(volume_id, operation_id)
        if self.lose_after_commit:
            self.lose_after_commit = False
            raise VolumeReplyLost('volume committed before its reply was interrupted')
        return state


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='bend-regression-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'archive'
        initialize(self.root)
        self.desk = Archive(self.root)
        self.addCleanup(self.desk.close)

    def original(self, original_id, desk=None):
        session = self.desk if desk is None else desk
        row = IntakeLedger(session.db).find_original(original_id)
        self.assertIsNotNone(row)
        return row

    def publish_b_copy(self, desk=None):
        session = self.desk if desk is None else desk
        body = self.original('B', session)
        session.volumes.provision_volume('V1', {'02': body})
        return session.register('B', VolumeLocation('V1', '02'), body)

    def withdraw_warmup(self, desk=None):
        session = self.desk if desk is None else desk
        return session.revoke('QC-X', ['X1', 'X2'])

    def add_batch(self, batch_id, volume_id, rows: list[OriginalRow], *, volume_slots=None):
        manifest = self.desk.load(batch_id, rows)
        placed = {str(entry.position): entry.original for entry in manifest.entries}
        if volume_slots is not None:
            placed = {slot: body for slot, body in placed.items() if slot in volume_slots}
        self.desk.volumes.provision_volume(volume_id, placed)
        for slot, row in placed.items():
            self.desk.register(row.original_id, VolumeLocation(volume_id, slot), row)
        return manifest

    def assert_good_receipt(self, receipt):
        self.assertEqual([
            (line.position, line.original_id, line.source, line.reading) for line in receipt.lines
        ], [(0, 'A', 'bend/P/01', 7), (2, 'B', 'bend/P/03', 11)])
        self.assertEqual(receipt.total, 18)
        self.assertIs(type(receipt.total), int)


def receipt_details(receipt):
    return {'ids': [line.original_id for line in receipt.lines], 'total': receipt.total}


def emit_scenario(case_id, *, input_data, expected, actual, downstream_expected, downstream_actual,
                  code_paths, oracle):
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id, 'input': input_data, 'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': code_paths, 'oracle': oracle
    }, sort_keys=True))
