from pathlib import Path

from archive import Archive, initialize
from copies.catalog import CopyCatalog
from copies.volume import VolumeLocation, VolumeState
from intake.ledger import IntakeLedger
from intake.rows import OriginalRow
from storage import WorkspaceError
from tests.fixtures import ArchiveCase, emit_scenario, receipt_details


class IntakeTests(ArchiveCase):
    def test_import_keeps_source_order_and_is_repeatable(self):
        rows = [OriginalRow('Z-02', 'bend/ORDER/01', 7), OriginalRow('A-01', 'bend/ORDER/02', 11),
                OriginalRow('Z-02', 'bend/ORDER/01', 7), OriginalRow('M-03', 'bend/ORDER/04', None)]
        accepted = self.add_batch('ORDER', 'ORDER-V', rows)
        replayed = self.desk.load('ORDER', iter(rows))
        self.assertEqual(accepted, replayed)
        expected = [[0, 'Z-02'], [1, 'A-01'], [3, 'M-03']]
        stored = IntakeLedger(self.desk.db).read_manifest('ORDER')
        actual = [[entry.position, entry.original.original_id] for entry in stored.entries]
        self.assertEqual(actual, expected)
        self.assertEqual(self.desk.db.execute("SELECT count(*) FROM originals WHERE original_id IN ('Z-02', 'A-01', 'M-03')").fetchone()[0], 3)
        signed = self.desk.get('ORDER')
        downstream_expected = {'ids': ['Z-02', 'A-01', 'M-03'], 'total': 18}
        self.assertEqual(receipt_details(signed), downstream_expected)
        self.assertEqual([line.position for line in signed.lines], [0, 1, 3])
        emit_scenario('v1', input_data=['Z-02', 'A-01', 'Z-02', 'M-03'], expected=expected, actual=actual,
                      downstream_expected=downstream_expected, downstream_actual=receipt_details(signed),
                      code_paths=['intake/manifest.py', 'intake/ledger.py', 'delivery/retrieve.py'],
                      oracle='Keep first source indices; add the two measured values 7 and 11.')

    def test_invalid_reading_rejects_whole_batch(self):
        for reading in (True, False, 11.0, 2.5, '11'):
            with self.subTest(reading=reading):
                good = OriginalRow('FRESH', 'bend/BAD/01', 7)
                bad = {'original_id': 'NOT-A-NUMBER', 'source': 'bend/BAD/02', 'reading': reading}
                with self.assertRaises(ValueError):
                    self.desk.load('BAD', [good, bad])
                ledger = IntakeLedger(self.desk.db)
                self.assertIsNone(ledger.read_manifest('BAD'))
                self.assertIsNone(ledger.find_original('FRESH'))

    def test_conflicting_original_rejects_batch(self):
        with self.assertRaises(ValueError):
            self.desk.load('CONFLICT', [OriginalRow('A', 'bend/CONFLICT/01', 7)])
        ledger = IntakeLedger(self.desk.db)
        self.assertIsNone(ledger.read_manifest('CONFLICT'))
        row = ledger.find_original('A')
        self.assertEqual(row, OriginalRow('A', 'bend/P/01', 7))
        signed = self.desk.get('P')
        downstream_expected = {'ids': ['A', 'X1', 'B', 'X2'], 'total': 23}
        self.assertEqual(receipt_details(signed), downstream_expected)
        emit_scenario('v2', input_data={'original_id': 'A', 'source': 'bend/CONFLICT/01', 'reading': 7},
                      expected='bend/P/01', actual=row.source,
                      downstream_expected=downstream_expected, downstream_actual=receipt_details(signed),
                      code_paths=['intake/validation.py', 'intake/import_batch.py', 'delivery/retrieve.py'],
                      oracle='A keeps its first source; original P still sums 7 + 2 + 11 + 3.')

    def test_no_partial_batch_after_late_conflict(self):
        fresh = OriginalRow('FRESH', 'bend/CONFLICT/01', -4)
        conflict = OriginalRow('B', 'bend/P/03', 12)
        registered = CopyCatalog(self.desk.db).list_for_original('B')
        with self.assertRaises(ValueError):
            self.desk.load('CONFLICT', [fresh, conflict])
        ledger = IntakeLedger(self.desk.db)
        self.assertIsNone(ledger.find_original('FRESH'))
        self.assertIsNone(ledger.read_manifest('CONFLICT'))
        self.assertEqual(CopyCatalog(self.desk.db).list_for_original('B'), registered)
        self.assertEqual(self.desk.get('P').total, 23)

    def test_same_batch_rejects_changed_manifest_and_reuses_original_elsewhere(self):
        rows = [self.original('B'), self.original('A')]
        manifest = self.desk.load('SECOND', rows)
        with self.assertRaises(ValueError):
            self.desk.load('SECOND', list(reversed(rows)))
        self.assertEqual(self.desk.load('SECOND', rows), manifest)
        self.assertEqual([line.original_id for line in self.desk.get('SECOND').lines], ['B', 'A'])

    def test_verified_slot_replay(self):
        row = self.original('A')
        location = VolumeLocation('V1', '01')
        before = CopyCatalog(self.desk.db).list_for_original('A')
        first = self.desk.register('A', location, row)
        self.assertEqual(first, self.desk.register('A', location, row))
        self.assertIn(first, before)
        self.assertEqual(CopyCatalog(self.desk.db).list_for_original('A'), before)

    def test_reopen_preserves_changes(self):
        rows = [OriginalRow('USER-9', 'bend/U/01', 9), OriginalRow('USER-1', 'bend/U/02', -1)]
        first_manifest = self.add_batch('USER', 'USER-V', rows)
        signed = self.desk.get('USER')
        self.withdraw_warmup()
        self.publish_b_copy()
        self.desk.retire('P-OLD', 'V0')
        self.desk.close()
        initialize(self.root)
        with Archive(self.root) as reopened:
            self.assertEqual(reopened.load('USER', rows), first_manifest)
            self.assertEqual(reopened.history(signed.receipt_id), signed)
            self.assertEqual(receipt_details(reopened.get('USER')), {'ids': ['USER-9', 'USER-1'], 'total': 8})
            self.assertEqual(reopened.db.execute('SELECT count(*) FROM batches').fetchone()[0], 4)
            self.assertEqual(reopened.volumes.inspect('V0'), VolumeState.DESTROYED)
            self.assertEqual(reopened.volumes.destruction_id('V0'), 'P-OLD')
            self.assert_good_receipt(reopened.get('P'))
            with self.assertRaises(ValueError):
                reopened.volumes.provision_volume('V0', {'01': self.original('A', reopened)})

    def test_missing_store_is_preserved_as_failure(self):
        self.desk.close()
        volume_file = self.root / 'volumes.sqlite3'
        volume_file.unlink()
        with self.assertRaises(WorkspaceError):
            initialize(self.root)
        self.assertFalse(volume_file.exists())
        self.assertTrue((self.root / 'catalog.sqlite3').is_file())

    def test_unfinished_initialization_does_not_replace_stores(self):
        partial = Path(self.root).parent / 'partial'
        partial.mkdir()
        existing = partial / 'catalog.sqlite3'
        existing.write_bytes(b'unfinished')
        with self.assertRaises(WorkspaceError):
            initialize(partial)
        with self.assertRaises(WorkspaceError):
            Archive(partial)
        self.assertEqual(existing.read_bytes(), b'unfinished')
        self.assertFalse((partial / 'volumes.sqlite3').exists())
