from intake.ledger import IntakeLedger
from intake.rows import OriginalRow
from quality.orders import WithdrawalOrder


def resolve_targets(ledger: IntakeLedger, order: WithdrawalOrder) -> tuple[OriginalRow, ...]:
    targets = []
    for original_id in order.original_ids:
        row = ledger.find_original(original_id)
        if row is None:
            raise KeyError(original_id)
        targets.append(row)
    return tuple(targets)
