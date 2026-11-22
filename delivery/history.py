import sqlite3

from delivery.receipt_store import Receipt, ReceiptStore


def history(db: sqlite3.Connection, receipt_id: int) -> Receipt:
    if type(receipt_id) is not int or receipt_id <= 0:
        raise ValueError('receipt_id must be a positive integer')
    if receipt_id > 2**63 - 1:
        raise KeyError(receipt_id)
    receipt = ReceiptStore(db).read_signed(receipt_id)
    if receipt is None:
        raise KeyError(receipt_id)
    return receipt
