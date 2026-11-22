from archive import Archive
from copies.volume import VolumeLocation, VolumeState
from intake.rows import OriginalRow
from retirement.journal import RetirementJournal, RetirementState
from tests.fixtures import ArchiveCase, InterruptedVolumeStore, emit_scenario, receipt_details


class HandoffTests(ArchiveCase):
    def retire_after_withdrawal(self, other):
        self.withdraw_warmup(other)
        self.publish_b_copy(other)
        result = other.retire('R0', 'V0')
        self.assertEqual(result.state, RetirementState.RETIRED)
        self.assertEqual(other.volumes.inspect('V0'), VolumeState.DESTROYED)

    def test_open_delivery_crosses_revoke_and_retirement(self):
        first = self.desk.get('P')
        with Archive(self.root) as other:
            def finish_work(manifest):
                self.assertEqual([entry.original.original_id for entry in manifest.entries], ['A', 'X1', 'B', 'X2'])
                self.withdraw_warmup(other)
                waiting = other.retire('R0', 'V0')
                self.assertEqual(waiting.state, RetirementState.WAITING)
                self.assertEqual(waiting.missing, ('B',))
                self.assertEqual(other.volumes.inspect('V0'), VolumeState.INTACT)
                self.assert_good_receipt(other.get('P'))
                self.publish_b_copy(other)
                self.assertEqual(other.retire('R0', 'V0').state, RetirementState.RETIRED)
                self.assertEqual(other.volumes.inspect('V0'), VolumeState.DESTROYED)

            crossed = self.desk.get('P', on_open=finish_work)
            self.assert_good_receipt(crossed)
        old = self.desk.history(first.receipt_id)
        self.assertEqual(old, first)
        emit_scenario('v5', input_data={'pause': 'open', 'withdrawn': ['X1', 'X2'], 'retired': 'V0'},
                      expected={'ids': ['A', 'B'], 'total': 18}, actual=receipt_details(crossed),
                      downstream_expected={'ids': ['A', 'X1', 'B', 'X2'], 'total': 23},
                      downstream_actual=receipt_details(old),
                      code_paths=['delivery/retrieve.py', 'quality/apply.py', 'retirement/request.py'],
                      oracle='New delivery sums 7 + 11; the original signature sums 7 + 2 + 11 + 3.')

    def test_delivery_crosses_retirement_before_b_read(self):
        first = self.desk.get('P')
        visited = []
        with Archive(self.root) as other:
            def pause_at_b(entry):
                visited.append(entry.original.original_id)
                if entry.original.original_id == 'B':
                    self.assertEqual(visited, ['A', 'X1', 'B'])
                    self.assertEqual(other.volumes.inspect('V0'), VolumeState.INTACT)
                    self.retire_after_withdrawal(other)

            crossed = self.desk.get('P', before_read=pause_at_b)
        self.assertEqual(visited, ['A', 'X1', 'B', 'X2'])
        self.assert_good_receipt(crossed)
        self.assertEqual(self.desk.history(first.receipt_id), first)

    def test_delivery_drops_cached_revoked_rows_after_fetch(self):
        first = self.desk.get('P')
        with Archive(self.root) as other:
            def pause_with_all_bodies(bodies):
                self.assertEqual([(row.original_id, row.reading) for row in bodies],
                                 [('A', 7), ('X1', 2), ('B', 11), ('X2', 3)])
                self.retire_after_withdrawal(other)

            crossed = self.desk.get('P', after_fetch=pause_with_all_bodies)
        self.assert_good_receipt(crossed)
        self.assertEqual(self.desk.history(first.receipt_id), first)

    def test_withdrawal_is_named_and_replays_its_target_set(self):
        order = self.desk.revoke('QC-X', ['X2', 'X1', 'X1'])
        self.assertEqual(order.original_ids, ('X1', 'X2'))
        self.assertEqual(order, self.desk.revoke('QC-X', ['X1', 'X2']))
        with self.assertRaises(ValueError):
            self.desk.revoke('QC-X', ['A', 'X2'])
        self.assertEqual(self.desk.retire('R0', 'V0').missing, ('B',))
        self.assert_good_receipt(self.desk.get('P'))
        self.assertEqual(receipt_details(self.desk.get('CAL')), {'ids': ['C'], 'total': 11})

    def test_unknown_withdrawal_target_is_atomic(self):
        with self.assertRaises(KeyError):
            self.desk.revoke('QC-X', ['X1', 'X2', 'UNKNOWN'])
        self.assertIsNone(self.desk.db.execute("SELECT 1 FROM withdrawal_orders WHERE order_id = 'QC-X'").fetchone())
        self.assertEqual(receipt_details(self.desk.get('P')), {'ids': ['A', 'X1', 'B', 'X2'], 'total': 23})
        self.assertEqual(self.withdraw_warmup().original_ids, ('X1', 'X2'))
        self.assert_good_receipt(self.desk.get('P'))

    def test_lost_destruction_reply_recovers_on_reopen(self):
        self.withdraw_warmup()
        self.publish_b_copy()
        self.desk.close()
        with InterruptedVolumeStore(self.root / 'volumes.sqlite3') as port, Archive(self.root, port=port) as interrupted:
            port.lose_after_commit = True
            pending = interrupted.retire('R0', 'V0')
            self.assertEqual(pending.state, RetirementState.PENDING)
            self.assertIsNone(pending.outcome)
            self.assertEqual(interrupted.volumes.inspect('V0'), VolumeState.DESTROYED)
            self.assertEqual(interrupted.volumes.destruction_id('V0'), 'R0')
            self.assertEqual(RetirementJournal(interrupted.db).read_request('R0'), pending)
        with Archive(self.root) as reopened:
            result = reopened.retire('R0', 'V0')
            self.assertEqual(result.state, RetirementState.RETIRED)
            self.assertEqual(result.outcome, VolumeState.DESTROYED)
            self.assertEqual(result.operation_id, 'R0')
            self.assertEqual(reopened.retire('R0', 'V0'), result)
            alias = reopened.retire('LATER', 'V0')
            self.assertEqual(alias.operation_id, 'R0')
            self.assertEqual(alias.state, RetirementState.RETIRED)
            self.assertEqual(reopened.volumes.destruction_id('V0'), 'R0')
            signed = reopened.get('P')
            self.assert_good_receipt(signed)
            emit_scenario('v6', input_data={'request': 'R0', 'target': 'V0', 'reply': 'lost after physical commit'},
                          expected={'state': 'retired', 'operation_id': 'R0'},
                          actual={'state': result.state.value, 'operation_id': result.operation_id},
                          downstream_expected={'ids': ['A', 'B'], 'total': 18}, downstream_actual=receipt_details(signed),
                          code_paths=['copies/physical_store.py', 'retirement/reopen.py', 'delivery/retrieve.py'],
                          oracle='Independent volume state records R0 once; the two replacement readings are 7 and 11.')

    def test_unconfirmed_status_preserves_intent_and_registration_limit(self):
        self.withdraw_warmup()
        self.publish_b_copy()
        with InterruptedVolumeStore(self.root / 'volumes.sqlite3') as port, Archive(self.root, port=port) as other:
            port.interrupt_before_dispatch = True
            self.assertEqual(other.retire('R0', 'V0').state, RetirementState.PENDING)
            self.assertEqual(self.desk.volumes.inspect('V0'), VolumeState.INTACT)
            row = OriginalRow('EXTRA', 'bend/extras/01', 23)
            self.desk.load('EXTRA-BATCH', [row])
            self.desk.volumes.provision_volume('V0', {'05': row})
            with self.assertRaises(ValueError):
                self.desk.register('EXTRA', VolumeLocation('V0', '05'), row)
            port.unavailable_volume = 'V0'
            pending = other.retire('R0', 'V0')
            self.assertEqual(pending.state, RetirementState.PENDING)
            self.assertIsNone(pending.outcome)
            self.assertIsNone(self.desk.volumes.destruction_id('V0'))
            port.unavailable_volume = None
            self.assertEqual(other.retire('R0', 'V0').state, RetirementState.RETIRED)
            self.assertEqual(other.volumes.destruction_id('V0'), 'R0')

    def test_retirement_request_conflict_and_unknown_volume(self):
        initial = self.desk.retire('R0', 'V0')
        self.assertEqual(initial.state, RetirementState.WAITING)
        with self.assertRaises(ValueError):
            self.desk.retire('R0', 'V1')
        with self.assertRaises(KeyError):
            self.desk.retire('NO-SUCH-REQUEST', 'MISSING')
        journal = RetirementJournal(self.desk.db)
        self.assertIsNone(journal.read_request('NO-SUCH-REQUEST'))
        self.assertEqual(journal.read_request('R0'), initial)
