import argparse
import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from copies.catalog import CopyCatalog, CopyRecord
from copies.physical_store import LocalVolumeStore
from copies.register import register
from copies.volume import VolumeLocation, VolumePort, VolumeState
from delivery.history import history
from delivery.receipt_store import Receipt, ReceiptStore
from delivery.retrieve import get
from intake.import_batch import load
from intake.ledger import IntakeLedger
from intake.manifest import BatchManifest, ManifestEntry
from intake.rows import OriginalRow
from quality.apply import create_schema as create_withdrawal_schema
from quality.apply import revoke
from quality.orders import WithdrawalOrder
from retirement.journal import RetirementJournal, RetirementState
from retirement.request import RetirementRequest, retire
from storage import WorkspaceError, open_database, set_format, transaction


CATALOG_APPLICATION_ID = 0x42574131
SAMPLES = Path(__file__).resolve().parent / 'samples'
READY = '.archive-ready'
STARTING = '.archive-initializing'


class Archive:
    def __init__(self, root: str | Path, *, port: VolumePort | None = None):
        self.root = Path(root)
        if not (self.root / READY).is_file():
            raise WorkspaceError('archive initialization is incomplete or absent')
        self.db = open_database(self.root / 'catalog.sqlite3', CATALOG_APPLICATION_ID)
        try:
            RetirementJournal(self.db).upgrade_schema()
            self.volumes = LocalVolumeStore(self.root / 'volumes.sqlite3')
        except BaseException:
            self.db.close()
            raise
        self.port = self.volumes if port is None else port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        try:
            self.volumes.close()
        finally:
            self.db.close()

    def load(self, batch_id: str, rows: Iterable[OriginalRow | Mapping]) -> BatchManifest:
        return load(self.db, batch_id, rows)

    def register(self, original_id: str, location: VolumeLocation, candidate: OriginalRow | Mapping) -> CopyRecord:
        return register(self.db, self.port, original_id, location, candidate)

    def revoke(self, order_id: str, original_ids: Iterable[str]) -> WithdrawalOrder:
        return revoke(self.db, order_id, original_ids)

    def retire(self, request_id: str, volume_id: str, *,
               on_ready: Callable[[RetirementRequest], None] | None = None) -> RetirementRequest:
        return retire(self.db, self.port, request_id, volume_id, on_ready=on_ready)

    def get(self, batch_id: str, *, on_open: Callable[[BatchManifest], None] | None = None,
            before_read: Callable[[ManifestEntry], None] | None = None,
            after_fetch: Callable[[tuple[OriginalRow, ...]], None] | None = None) -> Receipt:
        return get(self.db, self.port, batch_id, on_open=on_open, before_read=before_read, after_fetch=after_fetch)

    def history(self, receipt_id: int) -> Receipt:
        return history(self.db, receipt_id)


def _sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding='utf-8'))


def _seed(db, volumes: LocalVolumeStore) -> None:
    for name in ('handoff.json', 'readings.json'):
        sample = _sample(name)
        rows = {}
        for batch in sample['batches']:
            manifest = load(db, batch['batch_id'], batch['rows'])
            rows.update((entry.original.original_id, entry.original) for entry in manifest.entries)
        for volume_id, placements in sample['volumes'].items():
            volumes.provision_volume(volume_id, {slot: rows[original_id] for slot, original_id in placements.items()})
            for slot, original_id in placements.items():
                register(db, volumes, original_id, VolumeLocation(volume_id, slot), rows[original_id])


def initialize(root: str | Path) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ready = root / READY
    if ready.is_file():
        with Archive(root):
            return
    claim = root / STARTING
    try:
        claim.touch(exist_ok=False)
    except FileExistsError as exc:
        if ready.is_file():
            with Archive(root):
                return
        raise WorkspaceError('archive initialization is unfinished') from exc

    # Another initializer may have committed and released the claim after our first check.
    if ready.is_file():
        claim.unlink()
        with Archive(root):
            return
    catalog_path = root / 'catalog.sqlite3'
    volumes_path = root / 'volumes.sqlite3'
    if catalog_path.exists() or volumes_path.exists():
        raise WorkspaceError('archive database files exist without a completion marker')
    with closing(open_database(catalog_path, CATALOG_APPLICATION_ID, create=True)) as db:
        with transaction(db, write=True):
            IntakeLedger(db).create_schema()
            CopyCatalog(db).create_schema()
            create_withdrawal_schema(db)
            RetirementJournal(db).create_schema()
            ReceiptStore(db).create_schema()
            set_format(db, CATALOG_APPLICATION_ID)
        with LocalVolumeStore(volumes_path, create=True) as volumes:
            _seed(db, volumes)
    ready.touch(exist_ok=False)
    claim.unlink()


def demo() -> None:
    with TemporaryDirectory(prefix='bend-archive-') as root:
        initialize(root)
        sample = _sample('handoff.json')
        with Archive(root) as desk:
            first = desk.get('P')
            during = {}

            def finish_day_shift(_manifest):
                desk.revoke(**sample['withdrawal'])
                waiting = desk.retire('SHIFT-R', 'V0')
                during['state'] = waiting.state.value
                during['missing'] = list(waiting.missing)
                during['volume'] = desk.volumes.inspect('V0').value
                during['total'] = desk.get('P').total
                replacement = sample['replacement']
                location = VolumeLocation(replacement['volume_id'], replacement['slot'])
                row = IntakeLedger(desk.db).find_original(replacement['original_id'])
                desk.volumes.provision_volume(location.volume_id, {location.slot: row})
                desk.register(row.original_id, location, row)
                result = desk.retire('SHIFT-R', 'V0')
                if result.state is not RetirementState.RETIRED or desk.volumes.inspect('V0') is not VolumeState.DESTROYED:
                    raise RuntimeError('the sample volume did not retire')

            signed = desk.get('P', on_open=finish_day_shift)
            original = desk.history(first.receipt_id)
            missing_sample = desk.get('S')
            expected = {'ids': ['A', 'B'], 'sources': ['bend/P/01', 'bend/P/03'], 'total': 18}
            actual = {'ids': [line.original_id for line in signed.lines],
                      'sources': [line.source for line in signed.lines], 'total': signed.total}
            downstream = {'ids': [line.original_id for line in original.lines], 'total': original.total}
            downstream_expected = {'ids': ['A', 'X1', 'B', 'X2'], 'total': 23}
            if during != {'state': 'waiting', 'missing': ['B'], 'volume': 'intact', 'total': 18}:
                raise RuntimeError(f'unexpected result while B awaits migration: {during}')
            if actual != expected or downstream != downstream_expected or original != first:
                raise RuntimeError('the sample receipts differ from the hand-checked results')
            if [line.reading for line in missing_sample.lines] != [7, None, 11] or missing_sample.total != 18:
                raise RuntimeError('the missing-reading sample differs from the hand-checked result')
            print('POINTWISE_SCENARIO ' + json.dumps({
                'case_id': 'v5', 'input': {'batch': 'P', 'withdrawal': sample['withdrawal'],
                                           'replacement': sample['replacement']},
                'expected': expected, 'actual': actual,
                'downstream_expected': downstream_expected, 'downstream_actual': downstream,
                'code_paths': ['delivery/retrieve.py', 'retirement/execute.py', 'delivery/history.py'],
                'oracle': 'Keep A and B in source order and add 7 + 11. First receipt sums 7 + 2 + 11 + 3.'
            }, sort_keys=True))
            print(json.dumps({'while_waiting': during, 'old_receipt': asdict(original),
                              'new_receipt': asdict(signed), 'missing_reading_receipt': asdict(missing_sample)},
                             sort_keys=True))
    from guard_scenario import demo_inventory
    demo_inventory()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Thin plate test records archive')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('init', help='initialize or reopen a workspace').add_argument('directory', type=Path)
    commands.add_parser('demo', help='run the sample workflow in a temporary workspace')
    args = parser.parse_args(argv)
    if args.action == 'init':
        initialize(args.directory)
    else:
        demo()


if __name__ == '__main__':
    main()
