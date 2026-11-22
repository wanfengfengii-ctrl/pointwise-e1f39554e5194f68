from archive import Archive
from copies.catalog import CopyCatalog
from copies.verify import CopyVerificationError
from copies.volume import VolumeLocation, VolumeReadError, VolumeState
from intake.rows import OriginalRow
from retirement.journal import RetirementState
from tests.fixtures import ArchiveCase, InterruptedVolumeStore, emit_scenario, receipt_details


class RetirementTests(ArchiveCase):
    def test_equal_value_is_not_a_copy(self):
        same_value = self.original('C')
        real_b = self.original('B')
        location = VolumeLocation('V1', '03')
        self.assertEqual(same_value.reading, real_b.reading)
        self.assertNotEqual(same_value.original_id, real_b.original_id)
        with self.assertRaises(CopyVerificationError):
            self.desk.register('B', location, same_value)
        self.withdraw_warmup()
        held = self.desk.retire('R0', 'V0')
        expected = {'missing': ['B'], 'state': 'waiting', 'volume': 'intact'}
        actual = {'missing': list(held.missing), 'state': held.state.value,
                  'volume': self.desk.volumes.inspect('V0').value}
        self.assertEqual(actual, expected)
        self.assertIsNone(self.desk.volumes.destruction_id('V0'))
        signed = self.desk.get('P')
        self.assert_good_receipt(signed)
        emit_scenario('v3', input_data={'target': 'V0', 'candidate': 'C', 'candidate_reading': 11,
                                       'withdrawn': ['X1', 'X2']}, expected=expected, actual=actual,
                      downstream_expected={'ids': ['A', 'B'], 'total': 18}, downstream_actual=receipt_details(signed),
                      code_paths=['copies/register.py', 'retirement/coverage.py', 'delivery/retrieve.py'],
                      oracle='C is a different original; V0 still supplies B. A plus B is 7 + 11.')

    def test_physical_body_mismatch_rejects_registration(self):
        location = VolumeLocation('V-MISMATCH', '01')
        self.desk.volumes.provision_volume(location.volume_id, {location.slot: OriginalRow('B', 'wrong-place', 11)})
        with self.assertRaises(CopyVerificationError):
            self.desk.register('B', location, self.original('B'))
        self.assertEqual(CopyCatalog(self.desk.db).list_for_volume(location.volume_id), ())
        self.withdraw_warmup()
        self.assertEqual(self.desk.retire('R0', 'V0').missing, ('B',))
        self.assert_good_receipt(self.desk.get('P'))

    def test_verified_copies_allow_retirement(self):
        self.withdraw_warmup()
        self.publish_b_copy()
        result = self.desk.retire('R0', 'V0')
        self.assertEqual(result.state, RetirementState.RETIRED)
        self.assertEqual(result.outcome, VolumeState.DESTROYED)
        self.assertEqual(result.evidence, (('A', VolumeLocation('V1', '01')), ('B', VolumeLocation('V1', '02'))))
        self.assertEqual(self.desk.volumes.destruction_id('V0'), 'R0')
        state = self.desk.volumes.inspect('V0')
        self.assertEqual(state, VolumeState.DESTROYED)
        with self.assertRaises(VolumeReadError):
            self.desk.volumes.read_slot(VolumeLocation('V0', '03'))
        self.assertEqual(len(CopyCatalog(self.desk.db).list_for_volume('V0')), 4)
        signed = self.desk.get('P')
        self.assert_good_receipt(signed)
        emit_scenario('v4', input_data={'target': 'V0', 'verified_off_volume': ['A', 'B']},
                      expected='destroyed', actual=state.value,
                      downstream_expected={'ids': ['A', 'B'], 'total': 18}, downstream_actual=receipt_details(signed),
                      code_paths=['retirement/coverage.py', 'retirement/execute.py', 'copies/physical_store.py'],
                      oracle='Both eligible identities have distinct replacement locations outside V0.')

    def test_same_volume_does_not_supply_independent_coverage(self):
        b = self.original('B')
        self.desk.volumes.provision_volume('V0', {'05': b})
        self.desk.register('B', VolumeLocation('V0', '05'), b)
        self.withdraw_warmup()
        progress = self.desk.retire('R0', 'V0')
        self.assertEqual(progress.state, RetirementState.WAITING)
        self.assertEqual(progress.missing, ('B',))
        self.assertEqual(self.desk.volumes.inspect('V0'), VolumeState.INTACT)
        self.assert_good_receipt(self.desk.get('P'))

    def test_competing_retirements_recheck_initial_coverage(self):
        row = OriginalRow('SHARED', 'bend/shared/01', 13)
        self.add_batch('SHARE', 'L', [row])
        self.desk.volumes.provision_volume('R', {'0': row})
        self.desk.register('SHARED', VolumeLocation('R', '0'), row)
        with Archive(self.root) as other:
            def let_r_commit(preliminary):
                self.assertEqual(preliminary.state, RetirementState.READY)
                self.assertEqual(preliminary.evidence, (('SHARED', VolumeLocation('R', '0')),))
                self.assertEqual(other.retire('DROP-R', 'R').state, RetirementState.RETIRED)

            first = self.desk.retire('DROP-L', 'L', on_ready=let_r_commit)
            self.assertEqual(first.state, RetirementState.WAITING)
            self.assertEqual(first.missing, ('SHARED',))
            self.assertEqual(other.volumes.inspect('L'), VolumeState.INTACT)
            self.assertEqual(other.volumes.inspect('R'), VolumeState.DESTROYED)
            self.assertEqual(other.get('SHARE').total, 13)

    def test_pending_intact_volume_no_longer_backs_another_retirement(self):
        row = OriginalRow('SHARED', 'bend/shared/01', 13)
        self.add_batch('SHARE', 'L', [row])
        self.desk.volumes.provision_volume('R', {'0': row})
        self.desk.register('SHARED', VolumeLocation('R', '0'), row)
        with InterruptedVolumeStore(self.root / 'volumes.sqlite3') as port, Archive(self.root, port=port) as other:
            port.interrupt_before_dispatch = True
            pending = other.retire('DROP-R', 'R')
            self.assertEqual(pending.state, RetirementState.PENDING)
            self.assertEqual(self.desk.volumes.inspect('R'), VolumeState.INTACT)
            first = self.desk.retire('DROP-L', 'L')
            self.assertEqual(first.missing, ('SHARED',))
            self.assertEqual(first.state, RetirementState.WAITING)
            self.assertEqual(other.retire('DROP-R', 'R').state, RetirementState.RETIRED)
        self.assertEqual(self.desk.volumes.inspect('L'), VolumeState.INTACT)
        self.assertEqual(self.desk.get('SHARE').total, 13)
