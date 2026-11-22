import sqlite3
from collections.abc import Iterable


class EligibilityReader:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def eligible_ids(self, original_ids: Iterable[str]) -> tuple[str, ...]:
        eligible = []
        for original_id in original_ids:
            withdrawn = self.db.execute('''
                SELECT 1 FROM withdrawal_targets WHERE original_id = ? LIMIT 1
            ''', (original_id,)).fetchone()
            if withdrawn is None:
                eligible.append(original_id)
        return tuple(eligible)
