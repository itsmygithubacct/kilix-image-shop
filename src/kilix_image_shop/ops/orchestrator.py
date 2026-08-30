"""Incremental provider-port dispatch with zero production I1 providers."""

from __future__ import annotations

from dataclasses import dataclass

from kilix_image_shop.domain.commands import ApplyOperationOutput
from kilix_image_shop.engine.api import CancelToken, CancelledOrStaleWork
from kilix_image_shop.ports import (
    OperationAvailabilityView,
    OperationProviderPort,
    OperationResultVerifierPort,
    OperationSessionPort,
)

from .messages import (
    ErrorOrigin,
    OperationCancel,
    OperationError,
    OperationErrorCode,
    OperationKind,
    OperationProgress,
    OperationRequest,
    OperationResult,
    ProviderAvailability,
)
from .state import OperationState, OperationStateError, OperationStatus


DEFAULT_PROVIDER_PORTS: tuple[OperationProviderPort, ...] = ()


class OperationOrchestratorError(RuntimeError):
    """Operation dispatch or registry state violates the I1 boundary."""


@dataclass(slots=True)
class _Entry:
    state: OperationState
    session: OperationSessionPort | None


class _NoResultVerifier:
    """Production I1 cannot verify a result because it has zero providers."""

    def verify(self, request, result, *, cancel) -> bool:
        cancel.raise_if_cancelled()
        return False


class OperationOrchestrator:
    """Own bounded sessions while publishing only immutable lifecycle values."""

    def __init__(
        self,
        providers: tuple[OperationProviderPort, ...],
        verifier: OperationResultVerifierPort,
        *,
        max_active_requests: int,
        max_retained_requests: int,
    ) -> None:
        if not isinstance(providers, tuple) or any(
            not isinstance(item, OperationProviderPort) for item in providers
        ):
            raise OperationOrchestratorError("provider registry must be an immutable port tuple")
        if not isinstance(verifier, OperationResultVerifierPort):
            raise OperationOrchestratorError("operation verifier does not implement its port")
        if (
            isinstance(max_active_requests, bool)
            or not isinstance(max_active_requests, int)
            or max_active_requests <= 0
        ):
            raise OperationOrchestratorError("active operation ceiling must be finite")
        if (
            isinstance(max_retained_requests, bool)
            or not isinstance(max_retained_requests, int)
            or max_retained_requests < max_active_requests
        ):
            raise OperationOrchestratorError(
                "retained operation ceiling must be finite and cover active work"
            )
        self._providers = providers
        self._verifier = verifier
        self._maximum = max_active_requests
        self._retained_maximum = max_retained_requests
        self._entries: dict[str, _Entry] = {}

    @classmethod
    def zero_provider(
        cls,
        *,
        max_active_requests: int,
        max_retained_requests: int,
    ) -> OperationOrchestrator:
        return cls(
            DEFAULT_PROVIDER_PORTS,
            _NoResultVerifier(),
            max_active_requests=max_active_requests,
            max_retained_requests=max_retained_requests,
        )

    @property
    def provider_count(self) -> int:
        return len(self._providers)

    @property
    def request_count(self) -> int:
        return len(self._entries)

    @property
    def active_request_count(self) -> int:
        return sum(item.session is not None for item in self._entries.values())

    def _registrations(
        self,
    ) -> tuple[tuple[ProviderAvailability, OperationProviderPort], ...]:
        values: list[tuple[ProviderAvailability, OperationProviderPort]] = []
        for provider in self._providers:
            try:
                availability = provider.availability()
            except Exception:
                continue
            if not isinstance(availability, ProviderAvailability):
                raise OperationOrchestratorError("provider returned untyped availability")
            values.append((availability, provider))
        identifiers = tuple(item[0].provider_id for item in values)
        if len(set(identifiers)) != len(identifiers):
            raise OperationOrchestratorError("provider registry contains duplicate identities")
        return tuple(sorted(values, key=lambda item: item[0].provider_id))

    def _availability(self) -> tuple[ProviderAvailability, ...]:
        return tuple(item[0] for item in self._registrations())

    def availability_views(self) -> tuple[OperationAvailabilityView, ...]:
        values = self._availability()
        result: list[OperationAvailabilityView] = []
        for operation in OperationKind:
            matching = tuple(item for item in values if operation in item.operations)
            if len(matching) > 1:
                raise OperationOrchestratorError(
                    "multiple providers claim one admitted operation"
                )
            provider_id = None if not matching else matching[0].provider_id
            result.append(
                OperationAvailabilityView(operation, provider_id is not None, provider_id)
            )
        return tuple(result)

    @staticmethod
    def _local_error(
        request: OperationRequest,
        code: OperationErrorCode,
        *,
        sequence: int = 0,
        retryable: bool = False,
    ) -> OperationError:
        return OperationError(
            request.request_id,
            sequence,
            code,
            ErrorOrigin.LOCAL,
            retryable,
        )

    def state(self, request_id: str) -> OperationState:
        entry = self._entries.get(request_id)
        if entry is None:
            raise OperationOrchestratorError("operation request is unknown")
        return entry.state

    def start(self, request: OperationRequest) -> OperationState:
        if not isinstance(request, OperationRequest):
            raise OperationOrchestratorError("operation start requires a typed request")
        if request.request_id in self._entries:
            raise OperationOrchestratorError("operation request ID is already retained")
        if self.request_count >= self._retained_maximum:
            raise OperationOrchestratorError(
                "retained operation population reached its explicit ceiling"
            )
        initial = OperationState.prepare(request)
        try:
            available = tuple(
                item
                for item in self._registrations()
                if request.operation in item[0].operations
            )
        except OperationOrchestratorError:
            refused = initial.refuse_local(
                self._local_error(request, OperationErrorCode.INTERNAL)
            )
            self._entries[request.request_id] = _Entry(refused, None)
            return refused
        if not available:
            refused = initial.refuse_local(
                self._local_error(
                    request,
                    OperationErrorCode.UNAVAILABLE,
                    retryable=True,
                )
            )
            self._entries[request.request_id] = _Entry(refused, None)
            return refused
        if len(available) != 1:
            refused = initial.refuse_local(
                self._local_error(request, OperationErrorCode.INTERNAL)
            )
            self._entries[request.request_id] = _Entry(refused, None)
            return refused
        if self.active_request_count >= self._maximum:
            refused = initial.refuse_local(
                self._local_error(
                    request,
                    OperationErrorCode.UNAVAILABLE,
                    retryable=True,
                )
            )
            self._entries[request.request_id] = _Entry(refused, None)
            return refused
        selected, provider = available[0]
        submitted = initial.submit(selected)
        try:
            session = provider.open(request)
            if not isinstance(session, OperationSessionPort):
                raise OperationOrchestratorError("provider returned an untyped session")
        except Exception:
            refused = submitted.refuse_local(
                self._local_error(
                    request,
                    OperationErrorCode.PROVIDER_FAILURE,
                    retryable=True,
                )
            )
            self._entries[request.request_id] = _Entry(refused, None)
            return refused
        self._entries[request.request_id] = _Entry(submitted, session)
        return submitted

    @staticmethod
    def _safe_close(session: OperationSessionPort | None) -> None:
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass

    def _finish_session(self, entry: _Entry) -> None:
        self._safe_close(entry.session)
        entry.session = None

    def poll(self, request_id: str, *, cancel: CancelToken) -> OperationState:
        if not isinstance(cancel, CancelToken):
            raise OperationOrchestratorError("operation poll requires cancellation state")
        entry = self._entries.get(request_id)
        if entry is None or entry.session is None:
            raise OperationOrchestratorError("operation has no active provider session")
        state = entry.state
        try:
            message = entry.session.receive()
            if isinstance(message, OperationProgress):
                state = state.progress(message)
            elif isinstance(message, (OperationResult, OperationError)):
                state = state.provider_terminal(message)
            else:
                raise OperationStateError("provider returned an unknown message type")
        except OperationStateError:
            state = state.refuse_local(
                self._local_error(
                    state.request,
                    OperationErrorCode.PROTOCOL_ERROR,
                    sequence=state.last_sequence,
                )
            )
        except Exception:
            if state.status in {
                OperationStatus.CANCELLING_UNKNOWN,
                OperationStatus.CANCEL_ACCEPTED,
            }:
                state = state.outcome_lost()
            else:
                state = state.refuse_local(
                    self._local_error(
                        state.request,
                        OperationErrorCode.PROVIDER_FAILURE,
                        sequence=state.last_sequence,
                        retryable=True,
                    )
                )
        if state.status is OperationStatus.VERIFYING and state.result is not None:
            try:
                cancel.raise_if_cancelled()
                accepted = self._verifier.verify(
                    state.request,
                    state.result,
                    cancel=cancel,
                )
                cancel.raise_if_cancelled()
                if accepted is not True:
                    accepted = False
                state = state.verify_result(accepted)
            except CancelledOrStaleWork:
                state = state.refuse_local(
                    self._local_error(
                        state.request,
                        OperationErrorCode.CANCELLED,
                        sequence=state.last_sequence,
                    )
                )
            except Exception:
                state = state.refuse_local(
                    self._local_error(
                        state.request,
                        OperationErrorCode.OUTPUT_INVALID,
                        sequence=state.last_sequence,
                    )
                )
        entry.state = state
        if state.terminal or state.status is OperationStatus.READY:
            self._finish_session(entry)
        return state

    def cancel_request(
        self,
        request_id: str,
        cancellation: OperationCancel,
    ) -> OperationState:
        entry = self._entries.get(request_id)
        if entry is None or entry.session is None:
            raise OperationOrchestratorError("operation has no cancellable session")
        state = entry.state.request_cancel(cancellation)
        entry.state = state
        try:
            outcome = entry.session.cancel(cancellation)
            state = state.cancellation_outcome(outcome)
        except Exception:
            state = state.outcome_lost()
        entry.state = state
        if state.terminal:
            self._finish_session(entry)
        return state

    def mark_outcome_lost(self, request_id: str) -> OperationState:
        entry = self._entries.get(request_id)
        if entry is None:
            raise OperationOrchestratorError("operation request is unknown")
        state = entry.state.outcome_lost()
        entry.state = state
        self._finish_session(entry)
        return state

    def commit(
        self,
        request_id: str,
        command: ApplyOperationOutput,
    ) -> ApplyOperationOutput:
        entry = self._entries.get(request_id)
        if entry is None:
            raise OperationOrchestratorError("operation request is unknown")
        state = entry.state.commit(command)
        entry.state = state
        return state.emitted_commands[0]

    def forget(self, request_id: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None:
            raise OperationOrchestratorError("operation request is unknown")
        if not entry.state.terminal:
            raise OperationOrchestratorError("non-terminal operation cannot be forgotten")
        self._finish_session(entry)
        del self._entries[request_id]

    def close(self) -> tuple[OperationState, ...]:
        retained: list[OperationState] = []
        for entry in self._entries.values():
            state = entry.state
            if state.status in {
                OperationStatus.CANCELLING_UNKNOWN,
                OperationStatus.CANCEL_ACCEPTED,
            }:
                state = state.outcome_lost()
            elif not state.terminal and state.status is not OperationStatus.READY:
                state = state.refuse_local(
                    self._local_error(
                        state.request,
                        OperationErrorCode.INTERNAL,
                        sequence=state.last_sequence,
                    )
                )
            entry.state = state
            self._finish_session(entry)
            retained.append(state)
        return tuple(retained)


__all__ = (
    "DEFAULT_PROVIDER_PORTS",
    "OperationOrchestrator",
    "OperationOrchestratorError",
)
