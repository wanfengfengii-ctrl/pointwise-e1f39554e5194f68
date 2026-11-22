import sqlite3
from collections.abc import Iterable

from intake.ledger import IntakeLedger
from quality.orders import WithdrawalOrder, parse_withdrawal_order
from quality.scope import resolve_targets
from storage import transaction


def create_schema(db: sqlite3.Connection) -> None:
    db.execute('CREATE TABLE withdrawal_orders (order_id TEXT PRIMARY KEY)')
    db.execute('''
        CREATE TABLE withdrawal_targets (
            order_id TEXT NOT NULL REFERENCES withdrawal_orders,
            original_id TEXT NOT NULL REFERENCES originals,
            PRIMARY KEY (order_id, original_id)
        )
    ''')
    db.execute('CREATE INDEX withdrawals_by_original ON withdrawal_targets (original_id)')


def revoke(db: sqlite3.Connection, order_id: str, original_ids: Iterable[str]) -> WithdrawalOrder:
    order = parse_withdrawal_order(order_id, original_ids)
    with transaction(db, write=True):
        previous = db.execute('SELECT 1 FROM withdrawal_orders WHERE order_id = ?', (order_id,)).fetchone()
        if previous is not None:
            stored = db.execute('''
                SELECT original_id FROM withdrawal_targets WHERE order_id = ? ORDER BY original_id
            ''', (order_id,))
            existing = WithdrawalOrder(order_id, tuple(row[0] for row in stored))
            if existing != order:
                raise ValueError(f'withdrawal order already has different targets: {order_id}')
            return existing
        targets = resolve_targets(IntakeLedger(db), order)
        db.execute('INSERT INTO withdrawal_orders VALUES (?)', (order_id,))
        db.executemany('INSERT INTO withdrawal_targets VALUES (?, ?)',
                       [(order_id, target.original_id) for target in targets])
    return order
