import sqlite3

from delivery.receipt_store import Receipt, ReceiptLine, ReceiptStore
from delivery.totals import sum_readings


def issue_receipt(db: sqlite3.Connection, batch_id: str, lines: tuple[ReceiptLine, ...]) -> Receipt:
    if not db.in_transaction:
        raise RuntimeError('signing requires the delivery publication transaction')
    return ReceiptStore(db).save_signed(batch_id, lines, sum_readings(lines))
