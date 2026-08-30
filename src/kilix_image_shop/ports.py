"""Native-, toolkit-, and provider-free application boundary protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kilix_image_shop.domain.commands import (
    CommandEffect,
    ReductionResult,
    ResolvedObject,
)
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import LayerId, ObjectId
from kilix_image_shop.engine.api import CancelToken
from kilix_image_shop.ops.messages import (
    CancellationOutcome,
    OperationCancel,
    OperationKind,
    OperationRequest,
    OperationResult,
    ProviderAvailability,
    ProviderMessage,
)


class PortContractError(RuntimeError):
    """A boundary value or adapter result violates its declared protocol."""


@dataclass(frozen=True, slots=True)
class ObjectPayload:
    reference: ResolvedObject
    payload: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ResolvedObject) or not isinstance(
            self.payload, bytes
        ):
            raise PortContractError("object payload requires reference and immutable bytes")
        if (
            len(self.payload) != self.reference.byte_count
            or ObjectId.from_bytes(self.payload) != self.reference.object_id
        ):
            raise PortContractError("object payload differs from its size or digest")


@runtime_checkable
class ObjectPort(Protocol):
    def write(self, value: ObjectPayload) -> None: ...

    def verify(self, reference: ResolvedObject) -> bool: ...

    def resolve(self, object_id: ObjectId) -> ResolvedObject: ...


@runtime_checkable
class EffectPort(Protocol):
    def publish(
        self,
        before: DocumentState,
        result: ReductionResult,
        effects: tuple[CommandEffect, ...],
    ) -> None: ...

    def restore(
        self,
        state: DocumentState,
        invalidated_layer_ids: tuple[LayerId, ...],
    ) -> None: ...


@runtime_checkable
class PresentationPort(Protocol):
    def document_changed(
        self,
        state: DocumentState,
        *,
        can_undo: bool,
        can_redo: bool,
    ) -> None: ...


@runtime_checkable
class OperationSessionPort(Protocol):
    def receive(self) -> ProviderMessage: ...

    def cancel(self, message: OperationCancel) -> CancellationOutcome: ...

    def close(self) -> None: ...


@runtime_checkable
class OperationProviderPort(Protocol):
    def availability(self) -> ProviderAvailability: ...

    def open(self, request: OperationRequest) -> OperationSessionPort: ...


@runtime_checkable
class OperationResultVerifierPort(Protocol):
    def verify(
        self,
        request: OperationRequest,
        result: OperationResult,
        *,
        cancel: CancelToken,
    ) -> bool: ...


@runtime_checkable
class ClockPort(Protocol):
    def now_utc(self) -> str: ...


@runtime_checkable
class ProjectPort(Protocol):
    def save(self, state: DocumentState) -> ObjectId: ...


@dataclass(frozen=True, slots=True)
class OperationAvailabilityView:
    operation: OperationKind
    available: bool
    provider_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, OperationKind) or not isinstance(
            self.available, bool
        ):
            raise PortContractError("operation availability view is malformed")
        if self.available != (self.provider_id is not None):
            raise PortContractError("operation availability lacks an exact provider state")


__all__ = (
    "ClockPort",
    "EffectPort",
    "ObjectPayload",
    "ObjectPort",
    "OperationAvailabilityView",
    "OperationProviderPort",
    "OperationResultVerifierPort",
    "OperationSessionPort",
    "PortContractError",
    "PresentationPort",
    "ProjectPort",
)
