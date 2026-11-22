from archive import Archive
from delivery.retrieve import DeliveryUnavailable
from intake.rows import OriginalRow
from retirement.journal import RetirementState
from tests.fixtures import ArchiveCase, emit_scenario, receipt_details


class DeliveryTests(ArchiveCase):
    def test_delivery_totals_keep_missing_rows(self):
        signed = self.desk.get('S')
        expected = {'readings': [7, None, 11], 'total': 18}
        actual = {'readings': [line.reading for line in signed.lines], 'total': signed.total}
        self.assertEqual(actual, expected)
        self.assertEqual([line.position for line in signed.lines], [0, 1, 2])
        self.assertEqual([line.source for line in signed.lines], ['bend/S/01', 'bend/S/02', 'bend/S/03'])
        reread = self.desk.history(signed.receipt_id)
        self.assertEqual(reread, signed)
        emit_scenario('v7', input_data={'batch': 'S', 'sequence': ['S7', 'SNA', 'S11']},
                      expected=expected, actual=actual,
                      downstream_expected=expected,
                      downstream_actual={'readings': [line.reading for line in reread.lines], 'total': reread.total},
                      code_paths=['delivery/totals.py', 'delivery/issue.py', 'delivery/receipt_store.py'],
                      oracle='7 + 11 equals 18, with the missing measurement kept in position 1.')

    def test_integer_precision_survives_issue_and_reopen(self):
        rows = [OriginalRow('BIG-A', 'bend/PREC/01', 9007199254740993),
                OriginalRow('BIG-M', 'bend/PREC/02', None),
                OriginalRow('BIG-B', 'bend/PREC/03', -9007199254740992)]
        self.add_batch('PREC', 'PREC-V', rows)
        signed = self.desk.get('PREC')
        self.assertEqual([line.reading for line in signed.lines], [9007199254740993, None, -9007199254740992])
        self.assertEqual(signed.total, 1)
        self.assertIs(type(signed.total), int)
        with Archive(self.root) as reopened:
            snapshot = reopened.history(signed.receipt_id)
            self.assertEqual(snapshot, signed)
            self.assertIs(type(snapshot.total), int)
            self.assertIs(type(snapshot.lines[0].reading), int)

    def test_values_outside_sqlite_integer_range_keep_exact_precision(self):
        value = 10**40
        rows = [OriginalRow('HUGE-A', 'bend/HUGE/01', value), OriginalRow('HUGE-B', 'bend/HUGE/02', 2)]
        self.add_batch('HUGE', 'HUGE-V', rows)
        signed = self.desk.get('HUGE')
        self.assertEqual(signed.total, 10**40 + 2)
        self.assertEqual(self.desk.history(signed.receipt_id).lines[0].reading, value)

    def test_zero_and_missing_rows_and_empty_eligible_batches(self):
        rows = [OriginalRow('Z', 'bend/Z/01', 0), OriginalRow('M', 'bend/Z/02', None)]
        self.add_batch('Z', 'Z-V', rows)
        zero = self.desk.get('Z')
        self.assertEqual([line.reading for line in zero.lines], [0, None])
        self.assertEqual(zero.total, 0)
        self.assertIs(type(zero.lines[0].reading), int)
        self.desk.revoke('DROP-Z', ['Z'])
        missing = self.desk.get('Z')
        self.assertEqual([(line.position, line.reading) for line in missing.lines], [(1, None)])
        self.assertEqual(missing.total, 0)
        self.desk.revoke('DROP-M', ['M'])
        empty = self.desk.get('Z')
        self.assertEqual(empty.lines, ())
        self.assertEqual(empty.total, 0)
        self.assertEqual(self.desk.history(empty.receipt_id), empty)

    def test_unreadable_effective_original_leaves_no_receipt(self):
        rows = [OriginalRow('READABLE', 'bend/PAIR/01', 7), OriginalRow('UNPLACED', 'bend/PAIR/02', 11)]
        self.add_batch('PAIR', 'PAIR-V', rows, volume_slots={'0'})
        previous = self.desk.get('S')
        with self.assertRaises(DeliveryUnavailable) as caught:
            self.desk.get('PAIR')
        self.assertEqual(caught.exception.original_ids, ('UNPLACED',))
        self.assertEqual(self.desk.db.execute('SELECT count(*) FROM signed_receipts').fetchone()[0], 1)
        self.assertEqual(self.desk.history(previous.receipt_id), previous)
        with self.assertRaises(KeyError):
            self.desk.get('UNKNOWN')
        self.assertEqual(self.desk.db.execute('SELECT count(*) FROM signed_receipts').fetchone()[0], 1)

    def test_unreadable_candidate_withdrawn_before_signing(self):
        rows = [OriginalRow('KEEP', 'bend/DROP/01', 7), OriginalRow('UNPLACED', 'bend/DROP/02', 11)]
        self.add_batch('DROP', 'DROP-V', rows, volume_slots={'0'})
        with Archive(self.root) as other:
            def withdraw_before_publication(bodies):
                self.assertEqual([body.original_id for body in bodies], ['KEEP'])
                other.revoke('QC-DROP', ['UNPLACED'])

            signed = self.desk.get('DROP', after_fetch=withdraw_before_publication)
        self.assertEqual(receipt_details(signed), {'ids': ['KEEP'], 'total': 7})

    def test_history_keeps_first_receipt_before_revocation(self):
        first = self.desk.get('P')
        expected = {'ids': ['A', 'X1', 'B', 'X2'], 'total': 23}
        self.assertEqual(receipt_details(first), expected)
        self.assertEqual([line.source for line in first.lines],
                         ['bend/P/01', 'warmup/X/01', 'bend/P/03', 'warmup/X/02'])
        self.withdraw_warmup()
        self.publish_b_copy()
        self.assertEqual(self.desk.retire('R0', 'V0').state, RetirementState.RETIRED)
        with Archive(self.root) as reopened:
            old = reopened.history(first.receipt_id)
            self.assertEqual(old, first)
            new = reopened.get('P')
            self.assert_good_receipt(new)
            self.assertNotEqual(new.receipt_id, old.receipt_id)
        emit_scenario('v8', input_data={'batch': 'P', 'first_receipt': first.receipt_id, 'retired': 'V0'},
                      expected=expected, actual=receipt_details(old),
                      downstream_expected={'ids': ['A', 'B'], 'total': 18}, downstream_actual=receipt_details(new),
                      code_paths=['delivery/history.py', 'delivery/receipt_store.py', 'delivery/retrieve.py'],
                      oracle='The first signature retains four original rows totaling 23; a fresh signature sums only 7 + 11.')

    def test_unknown_receipt_fails_without_new_receipt(self):
        with self.assertRaises(KeyError):
            self.desk.history(1)
        self.assertEqual(self.desk.db.execute('SELECT count(*) FROM signed_receipts').fetchone()[0], 0)
