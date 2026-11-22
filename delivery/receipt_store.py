import json
import sqlite3
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    position: int
    original_id: str
    source: str
    reading: int | None


@dataclass(frozen=True, slots=True)
class Receipt:
    receipt_id: int
    batch_id: str
    lines: tuple[ReceiptLine, ...]
    total: int


class ReceiptStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create_schema(self) -> None:
        self.db.execute('''
            CREATE TABLE signed_receipts (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL REFERENCES batches,
                snapshot TEXT NOT NULL
            )
        ''')

    def save_signed(self, batch_id: str, lines: tuple[ReceiptLine, ...], total: int) -> Receipt:
        body = json.dumps({'lines': [asdict(line) for line in lines], 'total': total}, ensure_ascii=False)
        inserted = self.db.execute('INSERT INTO signed_receipts (batch_id, snapshot) VALUES (?, ?)', (batch_id, body))
        return Receipt(inserted.lastrowid, batch_id, lines, total)

    def read_signed(self, receipt_id: int) -> Receipt | None:
        stored = self.db.execute('SELECT batch_id, snapshot FROM signed_receipts WHERE receipt_id = ?',
                                 (receipt_id,)).fetchone()
        if stored is None:
            return None
        batch_id, snapshot = stored
        body = json.loads(snapshot)
        return Receipt(receipt_id, batch_id, tuple(ReceiptLine(**row) for row in body['lines']), body['total'])
