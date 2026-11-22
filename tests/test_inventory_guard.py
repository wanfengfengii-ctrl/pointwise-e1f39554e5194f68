from dataclasses import asdict, replace

from archive import Archive
from copies.physical_store import LocalVolumeStore
from copies.volume import VolumeBusy, VolumeLocation, VolumeReplyLost, VolumeState
from guard_scenario import (A, B, C, BACKUP, REQUEST, TARGET, CatalogInterrupted,
                            ScriptedVolumeStore, prepare)
from intake.rows import OriginalRow
from intake.ledger import IntakeLedger
from retirement.journal import RetirementState
from tests.fixtures import ArchiveCase, emit_scenario, receipt_details


class InventoryGuardTests(ArchiveCase):
    def provision_target(self, volume_id, slots, **kwargs):
        self.desk.volumes.provision_volume(volume_id, slots, inventory_guard=True)

    def seated(self, extras=None, *, provision_target=None):
        """Seed the guard fixture; returns the old receipt and leaves B unseated on T."""
        extra_rows = extras or []
        first = prepare(self.desk, provision_target=provision_target or self.provision_target)
        if extra_rows:
            self.desk.load('GUARD-EVEN-MORE', extra_rows)
        return first

    def unregistered_b(self):
        self.desk.volumes.provision_volume(TARGET, {'1': B})

    def publish_backup_b(self, desk=None, *, slot='1', body=None):
        session = self.desk if desk is None else desk
        candidate = IntakeLedger(session.db).find_original('GUARD-B') if body is None else body
        location = VolumeLocation(BACKUP, slot)
        session.volumes.provision_volume(BACKUP, {slot: candidate})
        return session.register(candidate.original_id, location, candidate)

    def test_unregistered_slot_is_held_until_b_has_real_copy(self):
        first = self.seated()
        self.unregistered_b()
        waiting = self.desk.retire(REQUEST, TARGET)
        self.assertEqual(waiting.state, RetirementState.WAITING)
        self.assertEqual(waiting.missing, ('GUARD-B',))
        self.assertEqual(waiting.unverified_slots, ())
        self.assertEqual(self.desk.volumes.inspect(TARGET), VolumeState.INTACT)
        self.assertEqual(self.desk.volumes.slot_entries(TARGET), (('0', A), ('1', B)))
        self.assertEqual(self.desk.history(first.receipt_id), first)
        c_copy = self.desk.register(C.original_id, VolumeLocation(BACKUP, '2'), C)
        self.assertNotEqual(c_copy.original_id, B.original_id)
        self.assertEqual(self.desk.retire(REQUEST, TARGET).missing, ('GUARD-B',))
        emit_scenario('v9', input_data={'target': TARGET, 'unregistered_slot': '1',
                                        'missing': ['GUARD-B']},
                      expected={'state': 'waiting', 'missing': ['GUARD-B'], 'volume': 'intact'},
                      actual={'state': waiting.state.value, 'missing': list(waiting.missing),
                              'volume': self.desk.volumes.inspect(TARGET).value},
                      downstream_expected={'inventory': [['0', 'GUARD-A'], ['1', 'GUARD-B']],
                                           'total': 7},
                      downstream_actual={'inventory': [list(pair) for pair in waiting.inventory],
                                         'total': self.desk.history(first.receipt_id).total},
                      code_paths=['retirement/inventory.py', 'retirement/execute.py',
                                  'copies/physical_store.py'],
                      oracle='The unregistered B slot is checked and held for a real off-volume copy; '
                             'A 7 μm still reads from W.')

        self.publish_backup_b()
        retired = self.desk.retire(REQUEST, TARGET)
        self.assertEqual(retired.state, RetirementState.RETIRED)
        self.assertEqual(retired.operation_id, self.desk.volumes.destruction_id(TARGET))
        self.assertEqual(retired.operation_id, REQUEST)
        self.assertEqual(retired.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-B')))
        self.assertEqual(retired.evidence, (('GUARD-A', VolumeLocation(BACKUP, '0')),
                                            ('GUARD-B', VolumeLocation(BACKUP, '1'))))
        self.assertEqual(retired.unverified_slots, ())
        self.assertEqual(self.desk.volumes.inspect(TARGET), VolumeState.DESTROYED)
        old = self.desk.history(first.receipt_id)
        self.assertEqual(old, first)
        self.assertEqual(old.total, 7)

    def test_unverifiable_slot_blocks_retirement(self):
        self.seated()
        self.unregistered_b()
        self.desk.volumes.provision_volume(TARGET, {'2': OriginalRow('GHOST', 'bend/ghost/01', 5)})
        held = self.desk.retire(REQUEST, TARGET)
        self.assertEqual(held.state, RetirementState.WAITING)
        self.assertEqual(held.unverified_slots, ('2',))
        self.assertEqual(held.missing, ('GUARD-B',))
        self.assertEqual(self.desk.volumes.inspect(TARGET), VolumeState.INTACT)
        emit_scenario('v10', input_data={'target': TARGET, 'bad_slot': '2'},
                      expected={'state': 'waiting', 'unverified_slots': ['2']},
                      actual={'state': held.state.value, 'unverified_slots': list(held.unverified_slots)},
                      downstream_expected={'volume': 'intact'}, downstream_actual={'volume': self.desk.volumes.inspect(TARGET).value},
                      code_paths=['retirement/inventory.py', 'intake/ledger.py'],
                      oracle='A slot whose body is not a known intake row cannot be reconciled.')

        # Correcting the physical body to the known row lets the same request proceed.
        self.desk.volumes.db.execute('DELETE FROM volume_slots WHERE volume_id = ? AND slot = ?', (TARGET, '2'))
        self.desk.volumes.db.commit()
        self.desk.volumes.provision_volume(TARGET, {'2': self.original('GUARD-A')})
        self.publish_backup_b()
        retired = self.desk.retire(REQUEST, TARGET)
        self.assertEqual(retired.state, RetirementState.RETIRED)
        self.assertEqual(retired.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-B'), ('2', 'GUARD-A')))
        self.assertEqual(retired.evidence, (('GUARD-A', VolumeLocation(BACKUP, '0')),
                                            ('GUARD-B', VolumeLocation(BACKUP, '1'))))
    def test_withheld_slot_still_counts_after_role_reversal(self):
        # A judged-withdrawn slot on the target stays a physical slot and needs no copy.
        self.seated()
        self.desk.revoke('QC-GUARD', ['GUARD-C'])
        self.desk.volumes.provision_volume(TARGET, {'1': C})
        self.publish_backup_b()
        retired = self.desk.retire(REQUEST, TARGET)
        self.assertEqual(retired.state, RetirementState.RETIRED)
        self.assertEqual(retired.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-C')))
        self.assertNotIn('GUARD-C', retired.missing)

    def test_busy_append_reports_retryable_and_stays_atomic(self):
        self.seated()
        result = {}

        def machine_appends():
            with LocalVolumeStore(self.root / 'volumes.sqlite3') as machine:
                with self.assertRaises(VolumeBusy):
                    machine.provision_volume(TARGET, {'1': B})
                result['slots'] = machine.slot_entries(TARGET)

        self.desk.close()
        with self.assertRaises(CatalogInterrupted):
            with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                     before_destroy=machine_appends,
                                     interruption='rollback') as port, Archive(self.root, port=port) as other:
                other.retire(REQUEST, TARGET)
        self.assertEqual(result['slots'], (('0', A),))
        with Archive(self.root) as reopened:
            self.assertEqual(reopened.retire(REQUEST, TARGET).state, RetirementState.RETIRED)
            self.assertEqual(reopened.volumes.slot_entries(TARGET), ())

    def test_append_landing_after_preliminary_check_is_included(self):
        self.seated()

        def machine_appends(preliminary):
            self.assertEqual(preliminary.state, RetirementState.READY)
            self.desk.volumes.provision_volume(TARGET, {'1': B})

        held = self.desk.retire(REQUEST, TARGET, on_ready=machine_appends)
        self.assertEqual(held.state, RetirementState.WAITING)
        self.assertEqual(held.missing, ('GUARD-B',))
        self.assertEqual(held.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-B')))
        self.assertEqual(self.desk.volumes.inspect(TARGET), VolumeState.INTACT)

    def test_append_racing_the_committed_intent_hits_busy(self):
        self.seated()
        seen = {}
        case = self

        class AppendAtDispatch(LocalVolumeStore):
            def destroy(self, volume_id, operation_id, **kwargs):
                if volume_id == TARGET:
                    with case.assertRaises(VolumeBusy):
                        other.volumes.provision_volume(TARGET, {'1': B})
                    seen['slots'] = sorted(kwargs['authorized_slots'])
                return super().destroy(volume_id, operation_id, **kwargs)

        self.desk.close()
        with Archive(self.root) as other:
            self.publish_backup_b(other)
            with AppendAtDispatch(self.root / 'volumes.sqlite3') as port, Archive(self.root, port=port) as guarded:
                result = guarded.retire(REQUEST, TARGET)
        self.assertEqual(result.state, RetirementState.RETIRED)
        self.assertEqual(seen['slots'], [('0', 'GUARD-A')])

    def test_destruction_requires_authorization_covering_actual_slots(self):
        self.seated()
        self.unregistered_b()
        with self.assertRaises(ValueError):
            self.desk.volumes.destroy(TARGET, 'SNEAK')
        self.assertEqual(self.desk.volumes.inspect(TARGET), VolumeState.INTACT)
        with self.assertRaises(ValueError):
            self.desk.volumes.destroy(TARGET, 'SNEAK', authorized_slots=frozenset({('0', 'GUARD-A')}))
        self.assertEqual(self.desk.volumes.inspect(TARGET), VolumeState.INTACT)
        self.assertIsNone(self.desk.volumes.destruction_id(TARGET))
        self.desk.volumes.destroy(TARGET, 'LEGIT',
                                  authorized_slots=frozenset({('0', 'GUARD-A'), ('1', 'GUARD-B')}))
        self.assertEqual(self.desk.volumes.inspect(TARGET), VolumeState.DESTROYED)

    def test_append_after_destruction_is_rejected(self):
        self.seated()
        self.publish_backup_b()
        self.assertEqual(self.desk.retire(REQUEST, TARGET).state, RetirementState.RETIRED)
        with self.assertRaises(ValueError):
            self.desk.volumes.provision_volume(TARGET, {'9': B})

    def test_lost_reply_after_commit_recovers_inventory_and_evidence(self):
        self.seated()
        self.unregistered_b()
        self.publish_backup_b()
        self.desk.close()
        with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                 interruption='after') as port, Archive(self.root, port=port) as other:
            pending = other.retire(REQUEST, TARGET)
            self.assertEqual(pending.state, RetirementState.PENDING)
            self.assertEqual(other.volumes.inspect(TARGET), VolumeState.DESTROYED)
        with Archive(self.root) as reopened:
            settled = reopened.retire(REQUEST, TARGET)
            self.assertEqual(settled.state, RetirementState.RETIRED)
            self.assertEqual(settled.operation_id, reopened.volumes.destruction_id(TARGET))
            self.assertEqual(settled.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-B')))
            self.assertEqual(settled.evidence, (('GUARD-A', VolumeLocation(BACKUP, '0')),
                                                ('GUARD-B', VolumeLocation(BACKUP, '1'))))

    def test_catalog_interrupted_after_volume_commit_recovers(self):
        self.seated()
        self.unregistered_b()
        self.publish_backup_b()
        self.desk.close()
        with self.assertRaises(CatalogInterrupted):
            with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                     interruption='rollback') as port, Archive(self.root, port=port) as other:
                other.retire(REQUEST, TARGET)
        with Archive(self.root) as reopened:
            settled = reopened.retire(REQUEST, TARGET)
            self.assertEqual(settled.state, RetirementState.RETIRED)
            self.assertEqual(settled.operation_id, reopened.volumes.destruction_id(TARGET))
            self.assertEqual(settled.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-B')))

    def test_reopen_uses_actual_evidence_when_original_exited_coverage(self):
        self.seated()
        self.unregistered_b()
        self.publish_backup_b()
        self.desk.close()
        with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                 interruption='before') as port, Archive(self.root, port=port) as other:
            first = other.retire(REQUEST, TARGET)
            self.assertEqual(first.state, RetirementState.PENDING)
            self.assertEqual(other.volumes.inspect(TARGET), VolumeState.INTACT)
            self.assertEqual(first.evidence, (('GUARD-A', VolumeLocation(BACKUP, '0')),
                                              ('GUARD-B', VolumeLocation(BACKUP, '1'))))
        # Between the lost dispatch and the retry, the first off-volume copy leaves coverage
        # and a second real copy of B is registered instead.
        with Archive(self.root) as later:
            later.db.execute('DELETE FROM verified_copies WHERE volume_id = ? AND slot = ?', (BACKUP, '1'))
            later.db.commit()
            later.volumes.provision_volume('GUARD-W2', {'4': B})
            later.register('GUARD-B', VolumeLocation('GUARD-W2', '4'), B)
        with self.assertRaises(CatalogInterrupted):
            with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                     interruption='rollback') as port, Archive(self.root, port=port) as other:
                other.retire(REQUEST, TARGET)
        with Archive(self.root) as reopened:
            self.assertEqual(reopened.volumes.inspect(TARGET), VolumeState.DESTROYED)
            settled = reopened.retire(REQUEST, TARGET)
            self.assertEqual(settled.state, RetirementState.RETIRED)
            self.assertEqual(settled.operation_id, REQUEST)
            self.assertEqual(settled.evidence, (('GUARD-A', VolumeLocation(BACKUP, '0')),
                                                ('GUARD-B', VolumeLocation('GUARD-W2', '4'))))
            self.assertEqual(settled.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-B')))

    def test_unknown_status_holds_pending_then_same_request_proceeds(self):
        self.seated()
        self.publish_backup_b()
        with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                 interruption='before') as port, Archive(self.root, port=port) as other:
            pending = other.retire(REQUEST, TARGET)
            self.assertEqual(pending.state, RetirementState.PENDING)
            self.assertEqual(other.volumes.inspect(TARGET), VolumeState.INTACT)
        with Archive(self.root) as reopened:
            settled = reopened.retire(REQUEST, TARGET)
            self.assertEqual(settled.state, RetirementState.RETIRED)
            self.assertEqual(reopened.volumes.destruction_id(TARGET), REQUEST)

    def test_pending_confirmed_intact_with_gap_waits_for_the_same_request(self):
        self.seated()
        self.unregistered_b()
        self.publish_backup_b()
        self.desk.close()
        with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                 interruption='before') as port, Archive(self.root, port=port) as other:
            self.assertEqual(other.retire(REQUEST, TARGET).state, RetirementState.PENDING)
        # The off-volume B copy leaves coverage before the retry; the same request must
        # return to waiting rather than keep an intent it cannot cover.
        with Archive(self.root) as later:
            later.db.execute('DELETE FROM verified_copies WHERE volume_id = ? AND slot = ?', (BACKUP, '1'))
            later.db.commit()
            held = later.retire(REQUEST, TARGET)
            self.assertEqual(held.state, RetirementState.WAITING)
            self.assertEqual(held.missing, ('GUARD-B',))
            self.assertIsNone(held.operation_id)
            self.assertEqual(later.volumes.inspect(TARGET), VolumeState.INTACT)
            later.volumes.provision_volume('GUARD-W3', {'8': B})
            later.register('GUARD-B', VolumeLocation('GUARD-W3', '8'), B)
            settled = later.retire(REQUEST, TARGET)
            self.assertEqual(settled.state, RetirementState.RETIRED)
            self.assertEqual(settled.operation_id, REQUEST)
            self.assertEqual(settled.inventory, (('0', 'GUARD-A'), ('1', 'GUARD-B')))
            self.assertEqual(settled.evidence, (('GUARD-A', VolumeLocation(BACKUP, '0')),
                                                ('GUARD-B', VolumeLocation('GUARD-W3', '8'))))

    def test_same_request_id_different_volume_is_rejected(self):
        self.seated()
        self.desk.load('GUARD-EXTRA', [OriginalRow('GUARD-X', 'bend/guard/x', 3)])
        self.desk.volumes.provision_volume(BACKUP, {'9': self.original('GUARD-X')})
        first = self.desk.retire(REQUEST, TARGET)
        self.assertEqual(first.state, RetirementState.RETIRED)
        with self.assertRaises(ValueError):
            self.desk.retire(REQUEST, BACKUP)

    def test_legacy_register_writes_disallowed_while_retiring(self):
        # Once the intent is committed the target is closed for new registrations.
        self.seated()
        self.unregistered_b()
        self.publish_backup_b()
        self.desk.close()
        with self.assertRaises(CatalogInterrupted):
            with ScriptedVolumeStore(self.root / 'volumes.sqlite3', TARGET,
                                     before_destroy=lambda: None,
                                     interruption='rollback') as port, Archive(self.root, port=port) as other:
                other.retire(REQUEST, TARGET)
        with Archive(self.root) as reopened:
            reopened.retire(REQUEST, TARGET)
        with Archive(self.root) as late:
            with self.assertRaises(ValueError):
                late.register('GUARD-A', VolumeLocation(BACKUP, '7'), A)
    def test_guard_cannot_be_enabled_on_existing_ordinary_volume(self):
        row = self.original('A')
        with self.assertRaises(ValueError):
            self.desk.volumes.provision_volume('V0', {'01': row}, inventory_guard=True)
        # Appending to an already-guarded volume without repeating the flag keeps the mode.
        self.seated()
        self.desk.volumes.provision_volume(TARGET, {'1': B})
        self.assertTrue(self.desk.volumes.is_inventory_guarded(TARGET))
        self.desk.volumes.provision_volume(TARGET, {'2': self.original('GUARD-A')}, inventory_guard=False)
        self.assertTrue(self.desk.volumes.is_inventory_guarded(TARGET))
