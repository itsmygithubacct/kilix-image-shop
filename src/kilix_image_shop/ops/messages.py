"""Operation-neutral typed request, progress, result, error, and cancel values."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from kilix_image_shop.domain.geometry import MAX_COORDINATE
from kilix_image_shop.domain.identifiers import (
    DocumentId,
    LayerId,
    ObjectId,
    RevisionId,
)
from kilix_image_shop.domain.layers import Parameter


REQUEST_SCHEMA = "kilix.imageshop.operation-request/v1"
CANCEL_SCHEMA = "kilix.imageshop.operation-cancel/v1"
MAX_SEQUENCE = 2**32 - 1


class OperationMessageError(ValueError):
    """An operation-neutral message is malformed or outside the closed set."""


class OperationKind(StrEnum):
    GENERATE = "generate"
    REMOVE_BACKGROUND = "remove-background"


class ProgressStage(StrEnum):
    QUEUED = "queued"
    LOADING = "loading"
    RUNNING = "running"
    ENCODING = "encoding"


class OperationOutputKind(StrEnum):
    PIXELS = "pixels"
    MASK = "mask"


class OutputEncoding(StrEnum):
    RGBA_U16 = "RGBA u16"
    Y_U8 = "Y u8"


class OperationErrorCode(StrEnum):
    UNAVAILABLE = "operation.unavailable"
    INVALID_REQUEST = "operation.invalid-request"
    DEADLINE = "operation.deadline"
    CANCELLED = "operation.cancelled"
    PROVIDER_FAILURE = "operation.provider-failure"
    PROTOCOL_ERROR = "operation.protocol-error"
    OUTPUT_INVALID = "operation.output-invalid"
    INTERNAL = "operation.internal"


class ErrorOrigin(StrEnum):
    LOCAL = "local"
    PROVIDER = "provider"


class CancelDisposition(StrEnum):
    ACCEPTED = "accepted"
    TERMINAL_WON = "terminal-won"


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_CODE_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)+\Z")
_DIAGNOSTIC_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")


def _uuid4(value: object, field: str) -> str:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise OperationMessageError(f"{field} must be a canonical lowercase UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise OperationMessageError(f"{field} is not a UUID") from exc
    if str(parsed) != value or parsed.version != 4:
        raise OperationMessageError(f"{field} must be a canonical lowercase UUIDv4")
    return value


def _sequence(value: object, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= MAX_SEQUENCE
    ):
        raise OperationMessageError("operation sequence is outside uint32")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise OperationMessageError(f"{field} is not a canonical code")
    return value


def _diagnostic(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DIAGNOSTIC_RE.fullmatch(value) is None:
        raise OperationMessageError("diagnostic reference is not opaque and canonical")
    return value


def _positive(value: object, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperationMessageError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise OperationMessageError(f"{field} exceeds its finite limit")
    return value


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise OperationMessageError("operation message cannot be serialized") from exc


@dataclass(frozen=True, slots=True)
class OperationRequest:
    schema: str
    request_id: str
    operation: OperationKind
    document_id: DocumentId
    revision: RevisionId
    target_layer_id: LayerId | None
    target_fingerprint: ObjectId | None
    input_object_ids: tuple[ObjectId, ...]
    parameters: tuple[Parameter, ...]
    deadline_ms: int

    def __post_init__(self) -> None:
        if self.schema != REQUEST_SCHEMA:
            raise OperationMessageError("unsupported operation-request schema")
        _uuid4(self.request_id, "request ID")
        if not isinstance(self.operation, OperationKind):
            raise OperationMessageError("operation kind is outside the admitted set")
        if not isinstance(self.document_id, DocumentId) or not isinstance(
            self.revision, RevisionId
        ):
            raise OperationMessageError("operation request lacks document identity")
        if self.target_layer_id is not None and not isinstance(
            self.target_layer_id, LayerId
        ):
            raise OperationMessageError("operation target layer is untyped")
        if self.target_fingerprint is not None and not isinstance(
            self.target_fingerprint, ObjectId
        ):
            raise OperationMessageError("operation target fingerprint is untyped")
        if (self.target_layer_id is None) != (self.target_fingerprint is None):
            raise OperationMessageError("target layer and fingerprint must appear together")
        if not isinstance(self.input_object_ids, tuple) or any(
            not isinstance(item, ObjectId) for item in self.input_object_ids
        ):
            raise OperationMessageError("operation inputs must be immutable object IDs")
        if len(self.input_object_ids) > 1024:
            raise OperationMessageError("operation input population exceeds its limit")
        if self.input_object_ids != tuple(
            sorted(set(self.input_object_ids), key=lambda item: item.value)
        ):
            raise OperationMessageError("operation inputs must be sorted and unique")
        if not isinstance(self.parameters, tuple) or any(
            not isinstance(item, Parameter) for item in self.parameters
        ):
            raise OperationMessageError("operation parameters must be immutable and typed")
        if len(self.parameters) > 256:
            raise OperationMessageError("operation parameter population exceeds its limit")
        names = tuple(item.name for item in self.parameters)
        if names != tuple(sorted(set(names))):
            raise OperationMessageError("operation parameters must be sorted and unique")
        _positive(self.deadline_ms, "operation deadline", maximum=86_400_000)

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "requestId": self.request_id,
            "operation": self.operation.value,
            "documentId": self.document_id.value,
            "revisionId": self.revision.value,
            "targetLayerId": (
                None if self.target_layer_id is None else self.target_layer_id.value
            ),
            "targetFingerprintSha256": (
                None
                if self.target_fingerprint is None
                else self.target_fingerprint.value
            ),
            "inputSha256": [item.value for item in self.input_object_ids],
            "parameters": [item.to_data() for item in self.parameters],
            "deadlineMs": self.deadline_ms,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_data())

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    provider_id: str
    runtime_digest: ObjectId
    operations: tuple[OperationKind, ...]

    def __post_init__(self) -> None:
        _code(self.provider_id, "provider ID")
        if not isinstance(self.runtime_digest, ObjectId):
            raise OperationMessageError("provider runtime must be content-addressed")
        if not isinstance(self.operations, tuple) or any(
            not isinstance(item, OperationKind) for item in self.operations
        ):
            raise OperationMessageError("provider operations must be immutable and closed")
        if not self.operations:
            raise OperationMessageError("provider must admit at least one operation")
        if self.operations != tuple(sorted(set(self.operations), key=lambda item: item.value)):
            raise OperationMessageError("provider operations must be sorted and unique")


@dataclass(frozen=True, slots=True)
class OperationProgress:
    request_id: str
    sequence: int
    stage: ProgressStage
    progress_u16: int

    def __post_init__(self) -> None:
        _uuid4(self.request_id, "progress request ID")
        _sequence(self.sequence)
        if not isinstance(self.stage, ProgressStage):
            raise OperationMessageError("progress stage is outside the closed set")
        if (
            isinstance(self.progress_u16, bool)
            or not isinstance(self.progress_u16, int)
            or not 0 <= self.progress_u16 <= 65535
        ):
            raise OperationMessageError("progress value must be uint16")


@dataclass(frozen=True, slots=True)
class OperationResult:
    request_id: str
    sequence: int
    provider_id: str
    runtime_digest: ObjectId
    model_digest: ObjectId | None
    output_kind: OperationOutputKind
    output_digest: ObjectId
    byte_count: int
    width: int
    height: int
    encoding: OutputEncoding
    profile_digest: ObjectId | None
    semantics: str
    diagnostic_ref: str | None = None

    def __post_init__(self) -> None:
        _uuid4(self.request_id, "result request ID")
        _sequence(self.sequence)
        _code(self.provider_id, "result provider ID")
        if not isinstance(self.runtime_digest, ObjectId):
            raise OperationMessageError("result runtime must be content-addressed")
        if self.model_digest is not None and not isinstance(self.model_digest, ObjectId):
            raise OperationMessageError("result model must be content-addressed")
        if not isinstance(self.output_kind, OperationOutputKind) or not isinstance(
            self.output_digest, ObjectId
        ):
            raise OperationMessageError("result output identity is malformed")
        _positive(self.byte_count, "result byte count", maximum=4_294_967_296)
        _positive(self.width, "result width", maximum=MAX_COORDINATE)
        _positive(self.height, "result height", maximum=MAX_COORDINATE)
        if not isinstance(self.encoding, OutputEncoding):
            raise OperationMessageError("result encoding is outside the closed set")
        if self.output_kind is OperationOutputKind.MASK:
            if (
                self.encoding is not OutputEncoding.Y_U8
                or self.profile_digest is not None
                or self.semantics != "foreground-alpha"
                or self.byte_count != self.width * self.height
            ):
                raise OperationMessageError("mask result violates Y u8 foreground-alpha")
        else:
            if (
                self.encoding is not OutputEncoding.RGBA_U16
                or not isinstance(self.profile_digest, ObjectId)
                or self.semantics != "colour"
                or self.byte_count != self.width * self.height * 8
            ):
                raise OperationMessageError("pixel result violates profiled RGBA u16")
        _diagnostic(self.diagnostic_ref)


@dataclass(frozen=True, slots=True)
class OperationError:
    request_id: str
    sequence: int
    code: OperationErrorCode
    origin: ErrorOrigin
    retryable: bool
    diagnostic_ref: str | None = None

    def __post_init__(self) -> None:
        _uuid4(self.request_id, "error request ID")
        _sequence(self.sequence, allow_zero=True)
        if not isinstance(self.code, OperationErrorCode) or not isinstance(
            self.origin, ErrorOrigin
        ):
            raise OperationMessageError("operation error code or origin is outside its set")
        if not isinstance(self.retryable, bool):
            raise OperationMessageError("operation retryability must be boolean")
        _diagnostic(self.diagnostic_ref)


@dataclass(frozen=True, slots=True)
class OperationCancel:
    schema: str
    request_id: str
    cancellation_id: str

    def __post_init__(self) -> None:
        if self.schema != CANCEL_SCHEMA:
            raise OperationMessageError("unsupported operation-cancel schema")
        _uuid4(self.request_id, "cancel request ID")
        _uuid4(self.cancellation_id, "cancellation ID")

    def to_data(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "requestId": self.request_id,
            "cancellationId": self.cancellation_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_data())


@dataclass(frozen=True, slots=True)
class CancellationOutcome:
    request_id: str
    cancellation_id: str
    sequence: int
    disposition: CancelDisposition
    terminal_sequence: int | None

    def __post_init__(self) -> None:
        _uuid4(self.request_id, "cancel-outcome request ID")
        _uuid4(self.cancellation_id, "cancel-outcome cancellation ID")
        _sequence(self.sequence)
        if not isinstance(self.disposition, CancelDisposition):
            raise OperationMessageError("cancel disposition is outside the closed set")
        if self.disposition is CancelDisposition.ACCEPTED:
            if self.terminal_sequence is not None:
                raise OperationMessageError("accepted cancellation cannot reserve a terminal")
        else:
            if self.terminal_sequence is None:
                raise OperationMessageError("terminal-won cancellation must name a terminal")
            _sequence(self.terminal_sequence)
            if self.terminal_sequence >= self.sequence:
                raise OperationMessageError("terminal-won sequence must precede its outcome")


ProviderMessage: TypeAlias = OperationProgress | OperationResult | OperationError


__all__ = (
    "CANCEL_SCHEMA",
    "CancelDisposition",
    "CancellationOutcome",
    "ErrorOrigin",
    "MAX_SEQUENCE",
    "OperationCancel",
    "OperationError",
    "OperationErrorCode",
    "OperationKind",
    "OperationMessageError",
    "OperationOutputKind",
    "OperationProgress",
    "OperationRequest",
    "OperationResult",
    "OutputEncoding",
    "ProgressStage",
    "ProviderAvailability",
    "ProviderMessage",
    "REQUEST_SCHEMA",
)
