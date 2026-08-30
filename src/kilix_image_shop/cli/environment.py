"""Non-mutating readiness probes for the OD-7 engine group and I1 boundaries."""

from __future__ import annotations

import os
import pathlib
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from kilix_image_shop.engine import compatibility
from kilix_image_shop.engine.runtime import (
    DPKG_STATUS_PATH,
    OD7_PACKAGE_NAMES,
    observe_installed_group,
)
from kilix_image_shop.ops.messages import OperationKind
from kilix_image_shop.ops.orchestrator import OperationOrchestrator

from .configuration import (
    DEFAULT_MAX_ACTIVE_OPERATIONS,
    DEFAULT_MAX_RETAINED_OPERATIONS,
    GUI_TOOLKIT_SELECTION,
)


ABSENT = "absent"

EXPECTED_PACKAGE_VERSIONS: dict[str, str] = {
    "libbabl-0.1-0": compatibility.BABL_PACKAGE_VERSION,
    "libgegl-0.4-0t64": compatibility.GEGL_PACKAGE_VERSION,
    "python3-gi": compatibility.PYTHON_GI_PACKAGE_VERSION,
}


class ComponentState(StrEnum):
    """Closed readiness states; an absent component never renders as ready."""

    READY = "ready"
    MISSING = "missing"
    MISMATCHED = "mismatched"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class ComponentReport:
    component: str
    state: ComponentState
    required: bool
    expected: str
    observed: str

    def __post_init__(self) -> None:
        if not isinstance(self.component, str) or not self.component:
            raise ValueError("component name must be a non-empty string")
        if not isinstance(self.state, ComponentState):
            raise ValueError("component state is outside the closed set")
        if not isinstance(self.required, bool):
            raise ValueError("component requirement must be explicit")
        for value, field in ((self.expected, "expected"), (self.observed, "observed")):
            if not isinstance(value, str) or not value:
                raise ValueError(f"component {field} value must be a non-empty string")

    def to_data(self) -> dict[str, object]:
        return {
            "component": self.component,
            "expected": self.expected,
            "observed": self.observed,
            "required": self.required,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    components: tuple[ComponentReport, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.components, tuple) or any(
            not isinstance(item, ComponentReport) for item in self.components
        ):
            raise ValueError("readiness components must be an immutable typed tuple")
        names = tuple(item.component for item in self.components)
        if names != tuple(sorted(set(names))):
            raise ValueError("readiness components must be sorted and unique")

    @property
    def required_components(self) -> tuple[ComponentReport, ...]:
        return tuple(item for item in self.components if item.required)

    @property
    def required_total(self) -> int:
        return len(self.required_components)

    @property
    def required_ready(self) -> int:
        return sum(
            item.state is ComponentState.READY for item in self.required_components
        )

    @property
    def conventional_editing_ready(self) -> bool:
        return self.required_ready == self.required_total

    def to_data(self) -> dict[str, object]:
        return {
            "components": [item.to_data() for item in self.components],
            "requiredReady": self.required_ready,
            "requiredTotal": self.required_total,
            "conventionalEditingReady": self.conventional_editing_ready,
        }


def package_reports(
    status_path: pathlib.Path = DPKG_STATUS_PATH,
) -> tuple[ComponentReport, ...]:
    """Compare the installed OD-7 group against its accepted 3/3 identity."""

    installed = observe_installed_group(status_path)
    values: list[ComponentReport] = []
    for name in sorted(OD7_PACKAGE_NAMES):
        expected = EXPECTED_PACKAGE_VERSIONS[name]
        observed = installed.get(name)
        if observed is None:
            state = ComponentState.MISSING
        elif observed != expected:
            state = ComponentState.MISMATCHED
        else:
            state = ComponentState.READY
        values.append(
            ComponentReport(
                component=f"package.{name}",
                state=state,
                required=True,
                expected=expected,
                observed=ABSENT if observed is None else observed,
            )
        )
    return tuple(values)


def interpreter_reports(
    *,
    environ: Mapping[str, str] | None = None,
    isolated: int | None = None,
    gi_origin: pathlib.Path = compatibility.GI_ORIGIN,
) -> tuple[ComponentReport, ...]:
    """Report the startup preconditions the engine guard refuses to run without."""

    values = os.environ if environ is None else environ
    isolation = sys.flags.isolated if isolated is None else isolated
    tolerance = values.get("BABL_TOLERANCE")
    python_path = values.get("PYTHONPATH")
    try:
        origin_present = gi_origin.is_file()
    except OSError:
        origin_present = False
    return (
        ComponentReport(
            component="environment.BABL_TOLERANCE",
            state=(
                ComponentState.READY if tolerance == "0.0" else ComponentState.MISMATCHED
            ),
            required=True,
            expected="0.0",
            observed=ABSENT if tolerance is None else tolerance,
        ),
        ComponentReport(
            component="environment.PYTHONPATH",
            state=(
                ComponentState.READY if not python_path else ComponentState.MISMATCHED
            ),
            required=True,
            expected=ABSENT,
            observed=ABSENT if not python_path else "set",
        ),
        ComponentReport(
            component="interpreter.isolated",
            state=ComponentState.READY if isolation == 1 else ComponentState.MISMATCHED,
            required=True,
            expected="1",
            observed=str(isolation),
        ),
        ComponentReport(
            component="python-gi.origin",
            state=ComponentState.READY if origin_present else ComponentState.MISSING,
            required=True,
            expected=gi_origin.as_posix(),
            observed="present" if origin_present else ABSENT,
        ),
    )


def provider_reports() -> tuple[ComponentReport, ...]:
    """Report the 0/2 production adapters as deferred, never as a local pass."""

    orchestrator = OperationOrchestrator.zero_provider(
        max_active_requests=DEFAULT_MAX_ACTIVE_OPERATIONS,
        max_retained_requests=DEFAULT_MAX_RETAINED_OPERATIONS,
    )
    views = {view.operation: view for view in orchestrator.availability_views()}
    values: list[ComponentReport] = []
    for kind in sorted(OperationKind, key=lambda item: item.value):
        view = views.get(kind)
        available = view is not None and view.available
        values.append(
            ComponentReport(
                component=f"provider.{kind.value}",
                state=ComponentState.READY if available else ComponentState.DEFERRED,
                required=False,
                expected="I2A provider adapter",
                observed=(
                    view.provider_id
                    if view is not None and view.provider_id is not None
                    else ABSENT
                ),
            )
        )
    return tuple(values)


def presentation_reports() -> tuple[ComponentReport, ...]:
    """Report the contained-GUI toolkit as an owner decision, not a local choice."""

    selection = GUI_TOOLKIT_SELECTION
    return (
        ComponentReport(
            component="presentation.gui-toolkit",
            state=(
                ComponentState.READY if selection else ComponentState.DEFERRED
            ),
            required=False,
            expected="owner-selected contained toolkit",
            observed=selection if selection else ABSENT,
        ),
    )


def readiness(
    *,
    status_path: pathlib.Path = DPKG_STATUS_PATH,
    environ: Mapping[str, str] | None = None,
    isolated: int | None = None,
    gi_origin: pathlib.Path = compatibility.GI_ORIGIN,
) -> ReadinessReport:
    """Compose every probe into one sorted, non-mutating readiness population."""

    components = (
        package_reports(status_path)
        + interpreter_reports(
            environ=environ,
            isolated=isolated,
            gi_origin=gi_origin,
        )
        + provider_reports()
        + presentation_reports()
    )
    return ReadinessReport(
        tuple(sorted(components, key=lambda item: item.component))
    )


__all__ = (
    "ABSENT",
    "EXPECTED_PACKAGE_VERSIONS",
    "ComponentReport",
    "ComponentState",
    "ReadinessReport",
    "interpreter_reports",
    "package_reports",
    "presentation_reports",
    "provider_reports",
    "readiness",
)
