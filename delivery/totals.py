from collections.abc import Iterable
from typing import Protocol


class ReadingLine(Protocol):
    @property
    def reading(self) -> int | None:
        ...


def sum_readings(lines: Iterable[ReadingLine]) -> int:
    return sum(line.reading for line in lines if line.reading is not None)
