"""Pure per-request operation lifecycle and linearizable cancellation reducer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from kilix_image_shop.domain.commands import ApplyOperationOutput

from .messages import (
    CancelDisposition,
    CancellationOutcome,
    ErrorOrigin,
    OperationCancel,
    OperationError,
    OperationErrorCode,
    OperationOutputKind,
    OperationProgress,
    OperationRequest,
    OperationResult,
    ProgressStage,
    ProviderAvailability,
    ProviderMessage,
)


class OperationStateError(RuntimeError):
    """An operation lifecycle transition is invalid or non-linearizable."""


class OperationStatus(StrEnum):
    PREPARING = "preparing"
    SUBMITTED = "submitted"
    ACTIVE = "active"
    VERIFYING = "verifying"
    READY = "ready"
    COMMITTED = "committed"
    REFUSED = "refused"
    CANCELLING_UNKNOWN = "cancelling-unknown"
    CANCEL_ACCEPTED = "cancel-accepted"
    CANCELLED = "cancelled"
    TERMINAL_WON = "terminal-won"
    OUTCOME_LOST = "outcome-lost"


PROGRESS_TRANSITIONS: dict[ProgressStage, tuple[ProgressStage, ...]] = {
    ProgressStage.QUEUED: (
        ProgressStage.QUEUED,
        ProgressStage.LOADING,
        ProgressStage.RUNNING,
    ),
    ProgressStage.LOADING: (
        ProgressStage.LOADING,
        ProgressStage.RUNNING,
    ),
    ProgressStage.RUNNING: (
        ProgressStage.RUNNING,
        ProgressStage.ENCODING,
    ),
    ProgressStage.ENCODING: (ProgressStage.ENCODING,),
}


@dataclass(frozen=True, slots=True)
class OperationState:
    request: OperationRequest
    status: OperationStatus
    provider: ProviderAvailability | None = None
    last_sequence: int = 0
    stage: ProgressStage | None = None
    progress_u16: int = 0
    result: OperationResult | None = None
    error: OperationError | None = None
    cancellation: OperationCancel | None = None
    cancel_outcome: CancellationOutcome | None = None
    reserved_terminal_sequence: int | None = None
    pending_terminal: OperationResult | OperationError | None = None
    command: ApplyOperationOutput | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, OperationRequest) or not isinstance(
            self.status, OperationStatus
        ):
            raise OperationStateError("operation state requires request and closed status")
        if self.provider is not None and not isinstance(
            self.provider, ProviderAvailability
        ):
            raise OperationStateError("operation provider identity is malformed")
        if (
            isinstance(self.last_sequence, bool)
            or not isinstance(self.last_sequence, int)
            or not 0 <= self.last_sequence <= 2**32 - 1
        ):
            raise OperationStateError("operation last sequence is outside uint32")
        if self.stage is not None and not isinstance(self.stage, ProgressStage):
            raise OperationStateError("operation progress stage is malformed")
        if (
            isinstance(self.progress_u16, bool)
            or not isinstance(self.progress_u16, int)
            or not 0 <= self.progress_u16 <= 65535
        ):
            raise OperationStateError("operation progress is outside uint16")
        if self.result is not None and not isinstance(self.result, OperationResult):
            raise OperationStateError("operation result is malformed")
        if self.error is not None and not isinstance(self.error, OperationError):
            raise OperationStateError("operation error is malformed")
        if self.cancellation is not None and not isinstance(
            self.cancellation, OperationCancel
        ):
            raise OperationStateError("operation cancellation is malformed")
        if self.cancel_outcome is not None and not isinstance(
            self.cancel_outcome, CancellationOutcome
        ):
            raise OperationStateError("operation cancellation outcome is malformed")
        if self.pending_terminal is not None and not isinstance(
            self.pending_terminal, (OperationResult, OperationError)
        ):
            raise OperationStateError("operation pending terminal is malformed")
        if self.command is not None and not isinstance(
            self.command, ApplyOperationOutput
        ):
            raise OperationStateError("operation command is outside its only exit type")
        if self.command is not None and self.status is not OperationStatus.COMMITTED:
            raise OperationStateError("operation command exists before committed state")
        if self.status is OperationStatus.COMMITTED and self.command is None:
            raise OperationStateError("committed operation lacks its command")

    @classmethod
    def prepare(cls, request: OperationRequest) -> OperationState:
        return cls(request=request, status=OperationStatus.PREPARING)

    @property
    def terminal(self) -> bool:
        return self.status in {
            OperationStatus.COMMITTED,
            OperationStatus.REFUSED,
            OperationStatus.CANCELLED,
            OperationStatus.OUTCOME_LOST,
        }

    @property
    def emitted_commands(self) -> tuple[ApplyOperationOutput, ...]:
        return () if self.command is None else (self.command,)

    def submit(self, provider: ProviderAvailability) -> OperationState:
        if self.status is not OperationStatus.PREPARING:
            raise OperationStateError("only a preparing operation can be submitted")
        if not isinstance(provider, ProviderAvailability):
            raise OperationStateError("operation submission requires provider identity")
        if self.request.operation not in provider.operations:
            raise OperationStateError("provider does not admit the requested operation")
        return replace(
            self,
            status=OperationStatus.SUBMITTED,
            provider=provider,
            stage=ProgressStage.QUEUED,
        )

    def refuse_local(self, error: OperationError) -> OperationState:
        if self.terminal or self.status is OperationStatus.COMMITTED:
            raise OperationStateError("terminal operation cannot be refused again")
        if not isinstance(error, OperationError) or error.request_id != self.request.request_id:
            raise OperationStateError("local refusal does not join the operation request")
        if error.origin is not ErrorOrigin.LOCAL:
            raise OperationStateError("local refusal must carry a local error origin")
        return replace(
            self,
            status=OperationStatus.REFUSED,
            error=error,
            pending_terminal=None,
            reserved_terminal_sequence=None,
        )

    def _require_message(self, message: ProviderMessage) -> None:
        if not isinstance(message, (OperationProgress, OperationResult, OperationError)):
            raise OperationStateError("provider returned an untyped operation message")
        if message.request_id != self.request.request_id:
            raise OperationStateError("provider message names another request")
        if message.sequence <= self.last_sequence:
            raise OperationStateError("provider sequence is not strictly increasing")
        if isinstance(message, OperationError) and message.origin is not ErrorOrigin.PROVIDER:
            raise OperationStateError("provider terminal carries a non-provider origin")

    def _require_result_join(self, result: OperationResult) -> None:
        if self.provider is None or (
            result.provider_id != self.provider.provider_id
            or result.runtime_digest != self.provider.runtime_digest
        ):
            raise OperationStateError("operation result differs from its selected provider")
        expected_kind = {
            "generate": OperationOutputKind.PIXELS,
            "remove-background": OperationOutputKind.MASK,
        }[self.request.operation.value]
        if result.output_kind is not expected_kind:
            raise OperationStateError("operation result kind differs from its request")

    def progress(self, message: OperationProgress) -> OperationState:
        if self.status not in {
            OperationStatus.SUBMITTED,
            OperationStatus.ACTIVE,
            OperationStatus.CANCELLING_UNKNOWN,
        }:
            raise OperationStateError("progress is not allowed in the current state")
        self._require_message(message)
        current = ProgressStage.QUEUED if self.stage is None else self.stage
        if message.stage not in PROGRESS_TRANSITIONS[current]:
            raise OperationStateError("provider progress stage regressed or skipped illegally")
        if message.progress_u16 < self.progress_u16:
            raise OperationStateError("provider progress value decreased")
        return replace(
            self,
            status=(
                OperationStatus.CANCELLING_UNKNOWN
                if self.status is OperationStatus.CANCELLING_UNKNOWN
                else OperationStatus.ACTIVE
            ),
            last_sequence=message.sequence,
            stage=message.stage,
            progress_u16=message.progress_u16,
        )

    @staticmethod
    def _protocol_error(request_id: str, sequence: int) -> OperationError:
        return OperationError(
            request_id,
            sequence,
            OperationErrorCode.PROTOCOL_ERROR,
            ErrorOrigin.LOCAL,
            False,
        )

    def _resolve_terminal(
        self,
        terminal: OperationResult | OperationError,
        *,
        last_sequence: int,
    ) -> OperationState:
        if isinstance(terminal, OperationResult):
            return replace(
                self,
                status=OperationStatus.VERIFYING,
                last_sequence=last_sequence,
                result=terminal,
                error=None,
                pending_terminal=None,
                reserved_terminal_sequence=None,
            )
        if terminal.code is OperationErrorCode.CANCELLED:
            return replace(
                self,
                status=OperationStatus.CANCELLED,
                last_sequence=last_sequence,
                error=terminal,
                pending_terminal=None,
                reserved_terminal_sequence=None,
            )
        return replace(
            self,
            status=OperationStatus.REFUSED,
            last_sequence=last_sequence,
            error=terminal,
            pending_terminal=None,
            reserved_terminal_sequence=None,
        )

    def provider_terminal(
        self,
        terminal: OperationResult | OperationError,
    ) -> OperationState:
        if isinstance(terminal, OperationResult):
            self._require_result_join(terminal)
        elif isinstance(terminal, OperationError):
            if terminal.origin is not ErrorOrigin.PROVIDER:
                raise OperationStateError("provider terminal carries a non-provider origin")
        else:
            raise OperationStateError("provider returned an untyped operation terminal")
        if self.status is OperationStatus.TERMINAL_WON:
            if (
                terminal.request_id != self.request.request_id
                or terminal.sequence != self.reserved_terminal_sequence
            ):
                raise OperationStateError("terminal replay differs from its reservation")
            return self._resolve_terminal(terminal, last_sequence=self.last_sequence)
        if self.status not in {
            OperationStatus.SUBMITTED,
            OperationStatus.ACTIVE,
            OperationStatus.CANCELLING_UNKNOWN,
            OperationStatus.CANCEL_ACCEPTED,
        }:
            raise OperationStateError("provider terminal is not allowed in this state")
        self._require_message(terminal)
        if self.status is OperationStatus.CANCELLING_UNKNOWN:
            return replace(
                self,
                last_sequence=terminal.sequence,
                pending_terminal=terminal,
            )
        if self.status is OperationStatus.CANCEL_ACCEPTED:
            if not (
                isinstance(terminal, OperationError)
                and terminal.code is OperationErrorCode.CANCELLED
            ):
                return self.refuse_local(
                    self._protocol_error(self.request.request_id, terminal.sequence)
                )
        return self._resolve_terminal(terminal, last_sequence=terminal.sequence)

    def request_cancel(self, cancellation: OperationCancel) -> OperationState:
        if not isinstance(cancellation, OperationCancel) or (
            cancellation.request_id != self.request.request_id
        ):
            raise OperationStateError("cancellation does not join the operation request")
        if self.status is OperationStatus.CANCELLING_UNKNOWN:
            if self.cancellation == cancellation:
                return self
            raise OperationStateError("cancellation replay changed its exact bytes")
        if self.status not in {OperationStatus.SUBMITTED, OperationStatus.ACTIVE}:
            raise OperationStateError("operation cannot enter cancellation now")
        return replace(
            self,
            status=OperationStatus.CANCELLING_UNKNOWN,
            cancellation=cancellation,
        )

    def cancellation_outcome(self, outcome: CancellationOutcome) -> OperationState:
        if self.status is not OperationStatus.CANCELLING_UNKNOWN:
            raise OperationStateError("cancellation outcome is unexpected")
        if self.cancellation is None or (
            outcome.request_id != self.request.request_id
            or outcome.cancellation_id != self.cancellation.cancellation_id
        ):
            raise OperationStateError("cancellation outcome does not join its intent")
        if outcome.sequence <= self.last_sequence:
            raise OperationStateError("cancellation outcome sequence is not increasing")
        if outcome.disposition is CancelDisposition.ACCEPTED:
            if self.pending_terminal is not None:
                return self.refuse_local(
                    self._protocol_error(self.request.request_id, outcome.sequence)
                )
            return replace(
                self,
                status=OperationStatus.CANCEL_ACCEPTED,
                last_sequence=outcome.sequence,
                cancel_outcome=outcome,
            )
        if self.pending_terminal is not None:
            if self.pending_terminal.sequence != outcome.terminal_sequence:
                return self.refuse_local(
                    self._protocol_error(self.request.request_id, outcome.sequence)
                )
            candidate = replace(
                self,
                status=OperationStatus.TERMINAL_WON,
                last_sequence=outcome.sequence,
                cancel_outcome=outcome,
                reserved_terminal_sequence=outcome.terminal_sequence,
            )
            return candidate._resolve_terminal(
                self.pending_terminal,
                last_sequence=outcome.sequence,
            )
        return replace(
            self,
            status=OperationStatus.TERMINAL_WON,
            last_sequence=outcome.sequence,
            cancel_outcome=outcome,
            reserved_terminal_sequence=outcome.terminal_sequence,
        )

    def outcome_lost(self) -> OperationState:
        if self.status not in {
            OperationStatus.CANCELLING_UNKNOWN,
            OperationStatus.CANCEL_ACCEPTED,
        }:
            raise OperationStateError("only unresolved cancellation can lose its outcome")
        return replace(
            self,
            status=OperationStatus.OUTCOME_LOST,
            pending_terminal=None,
        )

    def verify_result(
        self,
        accepted: bool,
        *,
        error: OperationError | None = None,
    ) -> OperationState:
        if self.status is not OperationStatus.VERIFYING or self.result is None:
            raise OperationStateError("only a verifying result can become ready")
        if not isinstance(accepted, bool):
            raise OperationStateError("result verification must be boolean")
        if accepted:
            if error is not None:
                raise OperationStateError("accepted result cannot carry an error")
            return replace(self, status=OperationStatus.READY)
        if error is None:
            error = OperationError(
                self.request.request_id,
                self.last_sequence,
                OperationErrorCode.OUTPUT_INVALID,
                ErrorOrigin.LOCAL,
                False,
            )
        if error.request_id != self.request.request_id or error.origin is not ErrorOrigin.LOCAL:
            raise OperationStateError("verification refusal must be a joined local error")
        return replace(self, status=OperationStatus.REFUSED, error=error)

    def commit(self, command: ApplyOperationOutput) -> OperationState:
        if self.status is not OperationStatus.READY or self.result is None:
            raise OperationStateError("only a ready operation can emit a command")
        if not isinstance(command, ApplyOperationOutput):
            raise OperationStateError("operation exit must be ApplyOperationOutput")
        result = self.result
        provenance = command.provenance
        expected_operation = {
            "generate": "kilix.generate",
            "remove-background": "kilix.remove-background",
        }[self.request.operation.value]
        if (
            provenance.operation != expected_operation
            or provenance.provider != result.provider_id
            or provenance.runtime_digest != result.runtime_digest
            or provenance.model_digest != result.model_digest
        ):
            raise OperationStateError("operation command provenance differs from its result")
        pixel_mode = command.output_asset is not None or command.output_layer is not None
        mask_mode = command.output_mask is not None or command.target_layer_id is not None
        if pixel_mode == mask_mode:
            raise OperationStateError("operation command must carry exactly one output mode")
        if pixel_mode:
            if result.output_kind is not OperationOutputKind.PIXELS:
                raise OperationStateError("pixel command follows a non-pixel result")
            if command.output_asset is None or command.output_layer is None:
                raise OperationStateError("pixel operation command is incomplete")
            if (
                command.output_asset.digest != result.output_digest
                or command.output_asset.byte_count != result.byte_count
                or command.output_asset.width != result.width
                or command.output_asset.height != result.height
                or command.output_asset.profile_digest != result.profile_digest
            ):
                raise OperationStateError("pixel operation command differs from its result")
        else:
            if result.output_kind is not OperationOutputKind.MASK:
                raise OperationStateError("mask command follows a non-mask result")
            if command.output_mask is None or command.target_layer_id is None:
                raise OperationStateError("mask operation command is incomplete")
            if (
                command.output_mask.object_id != result.output_digest
                or command.output_mask.width != result.width
                or command.output_mask.height != result.height
                or command.target_layer_id != self.request.target_layer_id
            ):
                raise OperationStateError("mask operation command differs from its result")
        return replace(
            self,
            status=OperationStatus.COMMITTED,
            command=command,
        )


__all__ = (
    "OperationState",
    "OperationStateError",
    "OperationStatus",
    "PROGRESS_TRANSITIONS",
)
