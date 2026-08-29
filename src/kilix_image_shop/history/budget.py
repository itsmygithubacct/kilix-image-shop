"""Required finite history count, resident-byte, and spill-byte policy."""

from __future__ import annotations

from dataclasses import dataclass


class HistoryBudgetError(ValueError):
    """A history budget or accounting value is malformed."""


def _positive(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoryBudgetError(f"{field} must be a finite positive integer")
    return value


def _nonnegative(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoryBudgetError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class HistoryBudget:
    """The architecture's 3/3 mandatory history ceilings."""

    max_entries: int
    max_resident_bytes: int
    max_spill_bytes: int

    def __post_init__(self) -> None:
        _positive(self.max_entries, "max_entries")
        _positive(self.max_resident_bytes, "max_resident_bytes")
        _positive(self.max_spill_bytes, "max_spill_bytes")


@dataclass(frozen=True, slots=True)
class HistoryUsage:
    entries: int
    undoable_entries: int
    redoable_entries: int
    resident_bytes: int
    spill_bytes: int
    pruned_entries: int
    budget: HistoryBudget

    def __post_init__(self) -> None:
        for field in (
            "entries",
            "undoable_entries",
            "redoable_entries",
            "resident_bytes",
            "spill_bytes",
            "pruned_entries",
        ):
            _nonnegative(getattr(self, field), field)
        if self.entries != self.undoable_entries + self.redoable_entries:
            raise HistoryBudgetError("history entry accounting is inconsistent")
        if self.entries > self.budget.max_entries:
            raise HistoryBudgetError("history entry accounting exceeds its budget")
        if self.resident_bytes > self.budget.max_resident_bytes:
            raise HistoryBudgetError("resident history accounting exceeds its budget")
        if self.spill_bytes > self.budget.max_spill_bytes:
            raise HistoryBudgetError("spilled history accounting exceeds its budget")
