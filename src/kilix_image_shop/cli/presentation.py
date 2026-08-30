"""Deterministic text and canonical-JSON rendering of command view models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kilix_image_shop.store.layout import canonical_json_bytes


class PresentationError(ValueError):
    """A view model carries a value the command-line surface may not print."""


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


def _safe(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PresentationError(f"{field} must be a non-empty string")
    if any(ord(item) < 0x20 or ord(item) == 0x7F for item in value):
        raise PresentationError(f"{field} contains an unprintable character")
    return value


def counted(numerator: int, denominator: int) -> str:
    """Render one count with its population; a bare number is never printed."""

    for value, field in ((numerator, "numerator"), (denominator, "denominator")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PresentationError(f"{field} must be a non-negative integer")
    if numerator > denominator:
        raise PresentationError("numerator exceeds its population")
    return f"{numerator}/{denominator}"


@dataclass(frozen=True, slots=True)
class Report:
    """One command result: aligned rows for humans, canonical data for scripts."""

    name: str
    rows: tuple[tuple[str, str], ...]
    data: dict[str, object]

    def __post_init__(self) -> None:
        _safe(self.name, "report name")
        if not isinstance(self.rows, tuple):
            raise PresentationError("report rows must be an immutable tuple")
        for row in self.rows:
            if not isinstance(row, tuple) or len(row) != 2:
                raise PresentationError("report row must be one label and one value")
            _safe(row[0], "report label")
            _safe(row[1], "report value")
        if not isinstance(self.data, dict):
            raise PresentationError("report data must be a JSON object")


def render_text(report: Report) -> str:
    """Render aligned label/value lines in the exact order the command emitted."""

    if not isinstance(report, Report):
        raise PresentationError("text rendering requires a typed report")
    if not report.rows:
        return ""
    width = max(len(label) for label, _ in report.rows)
    lines = [f"{label.ljust(width)}  {value}" for label, value in report.rows]
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    """Render one canonical JSON object; byte-stable for the same view model."""

    if not isinstance(report, Report):
        raise PresentationError("JSON rendering requires a typed report")
    try:
        payload = canonical_json_bytes({"command": report.name, "result": report.data})
    except Exception as exc:
        raise PresentationError("report data is not canonically serializable") from exc
    return payload.decode("utf-8")


def render(report: Report, output_format: OutputFormat) -> str:
    if not isinstance(output_format, OutputFormat):
        raise PresentationError("output format is outside the closed set")
    if output_format is OutputFormat.JSON:
        return render_json(report)
    return render_text(report)


__all__ = (
    "OutputFormat",
    "PresentationError",
    "Report",
    "counted",
    "render",
    "render_json",
    "render_text",
)
