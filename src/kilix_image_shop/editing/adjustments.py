"""Closed adjustment definitions, parameter validation, and command builders."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from kilix_image_shop.domain.commands import AddLayer, ChangeAdjustment
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import LayerId, RevisionId
from kilix_image_shop.domain.layers import (
    Adjustment,
    AdjustmentId,
    AdjustmentLayer,
    Parameter,
    ParameterValue,
)


class AdjustmentValidationError(ValueError):
    """An adjustment is outside the closed conventional-editing contract."""


@dataclass(frozen=True, slots=True)
class ScalarRule:
    minimum: float
    maximum: float

    def validate(self, value: object, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AdjustmentValidationError(f"{field} must be numeric")
        parsed = float(value)
        if not math.isfinite(parsed) or not self.minimum <= parsed <= self.maximum:
            raise AdjustmentValidationError(f"{field} is outside its closed range")


SCALAR_RULES: dict[AdjustmentId, dict[str, ScalarRule]] = {
    AdjustmentId.EXPOSURE: {"stops": ScalarRule(-20.0, 20.0)},
    AdjustmentId.CONTRAST: {"amount": ScalarRule(0.0, 4.0)},
    AdjustmentId.LEVELS: {
        "gamma": ScalarRule(0.01, 10.0),
        "input-black": ScalarRule(0.0, 1.0),
        "input-white": ScalarRule(0.0, 1.0),
        "output-black": ScalarRule(0.0, 1.0),
        "output-white": ScalarRule(0.0, 1.0),
    },
    AdjustmentId.WHITE_BALANCE: {
        "temperature-k": ScalarRule(1000.0, 40000.0),
        "tint": ScalarRule(-1.0, 1.0),
    },
    AdjustmentId.SATURATION: {"amount": ScalarRule(0.0, 4.0)},
    AdjustmentId.HUE: {"degrees": ScalarRule(-180.0, 180.0)},
    AdjustmentId.SHARPEN: {
        "amount": ScalarRule(0.0, 10.0),
        "radius": ScalarRule(0.1, 100.0),
        "threshold": ScalarRule(0.0, 1.0),
    },
    AdjustmentId.BLUR: {"sigma": ScalarRule(0.1, 256.0)},
}


def _validate_curve(value: object) -> None:
    if not isinstance(value, tuple) or len(value) < 4 or len(value) % 2:
        raise AdjustmentValidationError("curve points must be at least two x/y pairs")
    if len(value) > 4096:
        raise AdjustmentValidationError("curve point population exceeds its bound")
    pairs: list[tuple[float, float]] = []
    for index in range(0, len(value), 2):
        x = value[index]
        y = value[index + 1]
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(
            x, (int, float)
        ) or not isinstance(y, (int, float)):
            raise AdjustmentValidationError("curve coordinates must be numeric")
        pair = (float(x), float(y))
        if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in pair):
            raise AdjustmentValidationError("curve coordinates must stay in [0, 1]")
        pairs.append(pair)
    if any(left[0] >= right[0] for left, right in zip(pairs, pairs[1:])):
        raise AdjustmentValidationError("curve x coordinates must increase strictly")


def validate_adjustment(adjustment: Adjustment) -> Adjustment:
    if not isinstance(adjustment, Adjustment):
        raise AdjustmentValidationError("adjustment must be typed")
    values = {item.name: item.value for item in adjustment.parameters}
    if adjustment.adjustment_id is AdjustmentId.CURVES:
        if set(values) != {"points"}:
            raise AdjustmentValidationError("curves requires exactly the points parameter")
        _validate_curve(values["points"])
        return adjustment
    rules = SCALAR_RULES.get(adjustment.adjustment_id)
    if rules is None or set(values) != set(rules):
        raise AdjustmentValidationError("adjustment parameters are missing or unknown")
    for name, rule in rules.items():
        rule.validate(values[name], name)
    if adjustment.adjustment_id is AdjustmentId.LEVELS:
        if float(values["input-black"]) >= float(values["input-white"]):
            raise AdjustmentValidationError("level input black must precede white")
        if float(values["output-black"]) > float(values["output-white"]):
            raise AdjustmentValidationError("level output black must not exceed white")
    return adjustment


def make_adjustment(
    adjustment_id: AdjustmentId,
    parameters: Mapping[str, ParameterValue],
) -> Adjustment:
    if not isinstance(adjustment_id, AdjustmentId) or not isinstance(parameters, Mapping):
        raise AdjustmentValidationError("adjustment builder inputs are malformed")
    return validate_adjustment(
        Adjustment(
            adjustment_id,
            tuple(Parameter(name, value) for name, value in parameters.items()),
        )
    )


def add_adjustment_layer(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
    name: str,
    adjustment: Adjustment,
    parent_id: LayerId | None,
    index: int,
) -> AddLayer:
    validate_adjustment(adjustment)
    return AddLayer(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer=AdjustmentLayer(
            layer_id=layer_id,
            name=name,
            adjustment=adjustment,
        ),
        parent_id=parent_id,
        index=index,
    )


def change_adjustment(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
    adjustment: Adjustment,
) -> ChangeAdjustment:
    validate_adjustment(adjustment)
    if not isinstance(state.layer_map.get(layer_id), AdjustmentLayer):
        raise AdjustmentValidationError("adjustment target is not an adjustment layer")
    return ChangeAdjustment(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer_id=layer_id,
        adjustment=adjustment,
    )
