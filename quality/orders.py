from collections.abc import Iterable
from dataclasses import dataclass

from intake.rows import require_name


@dataclass(frozen=True, slots=True)
class WithdrawalOrder:
    order_id: str
    original_ids: tuple[str, ...]


def parse_withdrawal_order(order_id: str, original_ids: Iterable[str]) -> WithdrawalOrder:
    require_name(order_id, 'order_id')
    if isinstance(original_ids, (str, bytes)) or not isinstance(original_ids, Iterable):
        raise ValueError('original_ids must be an iterable of original identifiers')
    targets = tuple(sorted({require_name(original_id, 'original_id') for original_id in original_ids}))
    if not targets:
        raise ValueError('a withdrawal order requires at least one target')
    return WithdrawalOrder(order_id, targets)
