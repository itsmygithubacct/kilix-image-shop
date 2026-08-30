from __future__ import annotations

import ast
import dataclasses
import pathlib
import unittest

from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.commands import ApplyOperationOutput
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import (
    OperationProvenance,
    Parameter,
    PixelLayer,
)
from kilix_image_shop.engine.api import CancelToken
from kilix_image_shop.ops.diagnostics import (
    diagnostic_catalogue,
    render_operation_error,
)
from kilix_image_shop.ops.messages import (
    CANCEL_SCHEMA,
    REQUEST_SCHEMA,
    CancelDisposition,
    CancellationOutcome,
    ErrorOrigin,
    OperationCancel,
    OperationError,
    OperationErrorCode,
    OperationKind,
    OperationMessageError,
    OperationOutputKind,
    OperationProgress,
    OperationRequest,
    OperationResult,
    OutputEncoding,
    ProgressStage,
    ProviderAvailability,
)
from kilix_image_shop.ops.orchestrator import (
    DEFAULT_PROVIDER_PORTS,
    OperationOrchestrator,
    OperationOrchestratorError,
)
from kilix_image_shop.ops.state import (
    PROGRESS_TRANSITIONS,
    OperationState,
    OperationStateError,
    OperationStatus,
)

from domain_fixtures import empty_document, object_id


REQUEST_ID = "11111111-1111-4111-8111-111111111111"
CANCELLATION_ID = "22222222-2222-4222-8222-222222222222"
OUTPUT_PAYLOAD = bytes(range(16))
OUTPUT_DIGEST = ObjectId.from_bytes(OUTPUT_PAYLOAD)
RUNTIME_DIGEST = object_id("8")


def request(
    *,
    request_id: str = REQUEST_ID,
    operation: OperationKind = OperationKind.GENERATE,
) -> OperationRequest:
    document = empty_document()
    target = (
        None
        if operation is OperationKind.GENERATE
        else LayerId("00000000-0000-4000-8000-000000000001")
    )
    return OperationRequest(
        schema=REQUEST_SCHEMA,
        request_id=request_id,
        operation=operation,
        document_id=document.document_id,
        revision=document.revision_id,
        target_layer_id=target,
        target_fingerprint=None if target is None else object_id("7"),
        input_object_ids=(object_id("1"),),
        parameters=(Parameter("fixture-only", True),),
        deadline_ms=30_000,
    )


def availability() -> ProviderAvailability:
    return ProviderAvailability(
        "kilix.fake-provider",
        RUNTIME_DIGEST,
        (OperationKind.GENERATE, OperationKind.REMOVE_BACKGROUND),
    )


def pixel_result(sequence: int = 5) -> OperationResult:
    return OperationResult(
        request_id=REQUEST_ID,
        sequence=sequence,
        provider_id="kilix.fake-provider",
        runtime_digest=RUNTIME_DIGEST,
        model_digest=None,
        output_kind=OperationOutputKind.PIXELS,
        output_digest=OUTPUT_DIGEST,
        byte_count=16,
        width=2,
        height=1,
        encoding=OutputEncoding.RGBA_U16,
        profile_digest=object_id("1"),
        semantics="colour",
    )


def operation_command() -> ApplyOperationOutput:
    source = request()
    provenance = OperationProvenance(
        schema=OperationProvenance.SCHEMA,
        operation="kilix.generate",
        provider="kilix.fake-provider",
        model_digest=None,
        runtime_digest=RUNTIME_DIGEST,
        prompt=None,
        seed=None,
        parameters=(Parameter("fixture-only", True),),
        source_layer_digest=None,
        occurred_at="2026-08-30T00:00:00+00:00",
    )
    asset = AssetRef(
        digest=OUTPUT_DIGEST,
        byte_count=len(OUTPUT_PAYLOAD),
        media_type=MediaType.PNG,
        width=2,
        height=1,
        profile_digest=object_id("1"),
        import_policy=ImportPolicy.COPIED,
    )
    layer = PixelLayer(
        layer_id=LayerId("00000000-0000-4000-8000-000000000009"),
        name="generated",
        asset_digest=asset.digest,
        operation_provenance=provenance,
    )
    return ApplyOperationOutput(
        expected_revision=source.revision,
        new_revision=RevisionId("33333333-3333-4333-8333-333333333333"),
        provenance=provenance,
        output_asset=asset,
        output_layer=layer,
        parent_id=None,
        index=0,
    )


class MessageTests(unittest.TestCase):
    def test_request_is_canonical_digest_bound_and_closed_to_two_operations(self) -> None:
        value = request()
        self.assertEqual(value.canonical_bytes(), request().canonical_bytes())
        self.assertEqual(value.digest, ObjectId.from_bytes(value.canonical_bytes()))
        self.assertEqual(
            tuple(OperationKind),
            (OperationKind.GENERATE, OperationKind.REMOVE_BACKGROUND),
        )
        with self.assertRaises(OperationMessageError):
            request(request_id="not-a-uuid")
        with self.assertRaises(OperationMessageError):
            dataclasses.replace(value, deadline_ms=86_400_001)

    def test_pixel_and_mask_result_shapes_are_disjoint_and_exact(self) -> None:
        self.assertEqual(pixel_result().byte_count, 2 * 1 * 8)
        mask = OperationResult(
            request_id=REQUEST_ID,
            sequence=1,
            provider_id="kilix.fake-provider",
            runtime_digest=RUNTIME_DIGEST,
            model_digest=None,
            output_kind=OperationOutputKind.MASK,
            output_digest=ObjectId.from_bytes(b"\x00\x40\x80\xff"),
            byte_count=4,
            width=2,
            height=2,
            encoding=OutputEncoding.Y_U8,
            profile_digest=None,
            semantics="foreground-alpha",
        )
        self.assertEqual(mask.byte_count, 2 * 2)
        with self.assertRaises(OperationMessageError):
            OperationResult(
                request_id=REQUEST_ID,
                sequence=1,
                provider_id="kilix.fake-provider",
                runtime_digest=RUNTIME_DIGEST,
                model_digest=None,
                output_kind=OperationOutputKind.MASK,
                output_digest=mask.output_digest,
                byte_count=4,
                width=2,
                height=2,
                encoding=OutputEncoding.RGBA_U16,
                profile_digest=object_id("1"),
                semantics="colour",
            )


class LifecycleTests(unittest.TestCase):
    def test_exact_twelve_state_population_and_four_progress_rows(self) -> None:
        self.assertEqual(len(tuple(OperationStatus)), 12)
        self.assertEqual(set(PROGRESS_TRANSITIONS), set(ProgressStage))
        self.assertEqual(len(PROGRESS_TRANSITIONS), 4)

    def test_happy_result_emits_one_command_only_after_ready_commit(self) -> None:
        state = OperationState.prepare(request()).submit(availability())
        for sequence, stage, progress in (
            (1, ProgressStage.QUEUED, 0),
            (2, ProgressStage.LOADING, 1000),
            (3, ProgressStage.RUNNING, 32000),
            (4, ProgressStage.ENCODING, 60000),
        ):
            state = state.progress(
                OperationProgress(REQUEST_ID, sequence, stage, progress)
            )
        state = state.provider_terminal(pixel_result())
        self.assertEqual(state.status, OperationStatus.VERIFYING)
        self.assertEqual(state.emitted_commands, ())
        state = state.verify_result(True)
        self.assertEqual(state.status, OperationStatus.READY)
        self.assertEqual(state.emitted_commands, ())
        state = state.commit(operation_command())
        self.assertEqual(state.status, OperationStatus.COMMITTED)
        self.assertEqual(state.emitted_commands, (operation_command(),))

    def test_progress_regression_duplicate_sequence_and_second_terminal_refuse(self) -> None:
        state = OperationState.prepare(request()).submit(availability())
        state = state.progress(
            OperationProgress(REQUEST_ID, 1, ProgressStage.RUNNING, 100)
        )
        with self.assertRaises(OperationStateError):
            state.progress(
                OperationProgress(REQUEST_ID, 2, ProgressStage.LOADING, 200)
            )
        with self.assertRaises(OperationStateError):
            state.progress(
                OperationProgress(REQUEST_ID, 1, ProgressStage.RUNNING, 200)
            )
        terminal = state.provider_terminal(pixel_result(sequence=2))
        with self.assertRaises(OperationStateError):
            terminal.provider_terminal(pixel_result(sequence=3))

    def test_result_must_join_selected_provider_and_requested_output_kind(self) -> None:
        generated = OperationState.prepare(request()).submit(availability())
        with self.assertRaises(OperationStateError):
            generated.provider_terminal(
                dataclasses.replace(
                    pixel_result(sequence=1),
                    provider_id="kilix.other-provider",
                )
            )
        background = OperationState.prepare(
            request(operation=OperationKind.REMOVE_BACKGROUND)
        ).submit(availability())
        with self.assertRaises(OperationStateError):
            background.provider_terminal(pixel_result(sequence=1))

    def test_accepted_cancel_terminal_won_and_lost_outcome_emit_zero_commands(self) -> None:
        cancellation = OperationCancel(CANCEL_SCHEMA, REQUEST_ID, CANCELLATION_ID)
        base = OperationState.prepare(request()).submit(availability()).progress(
            OperationProgress(REQUEST_ID, 1, ProgressStage.RUNNING, 20000)
        )
        accepted = base.request_cancel(cancellation).cancellation_outcome(
            CancellationOutcome(
                REQUEST_ID,
                CANCELLATION_ID,
                2,
                CancelDisposition.ACCEPTED,
                None,
            )
        )
        accepted = accepted.provider_terminal(
            OperationError(
                REQUEST_ID,
                3,
                OperationErrorCode.CANCELLED,
                ErrorOrigin.PROVIDER,
                False,
            )
        )
        self.assertEqual(accepted.status, OperationStatus.CANCELLED)
        self.assertEqual(accepted.emitted_commands, ())

        pending = base.request_cancel(cancellation).provider_terminal(
            pixel_result(sequence=2)
        )
        won = pending.cancellation_outcome(
            CancellationOutcome(
                REQUEST_ID,
                CANCELLATION_ID,
                3,
                CancelDisposition.TERMINAL_WON,
                2,
            )
        )
        self.assertEqual(won.status, OperationStatus.VERIFYING)
        self.assertEqual(won.emitted_commands, ())

        lost = base.request_cancel(cancellation).outcome_lost()
        self.assertEqual(lost.status, OperationStatus.OUTCOME_LOST)
        self.assertEqual(lost.emitted_commands, ())

    def test_changed_cancel_replay_and_result_after_acceptance_fail_closed(self) -> None:
        base = OperationState.prepare(request()).submit(availability())
        first = OperationCancel(CANCEL_SCHEMA, REQUEST_ID, CANCELLATION_ID)
        cancelling = base.request_cancel(first)
        changed = OperationCancel(
            CANCEL_SCHEMA,
            REQUEST_ID,
            "44444444-4444-4444-8444-444444444444",
        )
        with self.assertRaises(OperationStateError):
            cancelling.request_cancel(changed)
        accepted = cancelling.cancellation_outcome(
            CancellationOutcome(
                REQUEST_ID,
                CANCELLATION_ID,
                1,
                CancelDisposition.ACCEPTED,
                None,
            )
        )
        refused = accepted.provider_terminal(pixel_result(sequence=2))
        self.assertEqual(refused.status, OperationStatus.REFUSED)
        self.assertEqual(refused.error.code, OperationErrorCode.PROTOCOL_ERROR)
        self.assertEqual(refused.emitted_commands, ())


class FakeSession:
    def __init__(self, messages, *, cancel_outcome=None, crash=False) -> None:
        self.messages = list(messages)
        self.cancel_outcome = cancel_outcome
        self.crash = crash
        self.closed = False

    def receive(self):
        if self.crash:
            raise RuntimeError("hostile provider prose /private/path")
        return self.messages.pop(0)

    def cancel(self, message):
        if self.cancel_outcome is None:
            raise RuntimeError("cancel outcome lost")
        return self.cancel_outcome

    def close(self) -> None:
        self.closed = True


class FakeProvider:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def availability(self):
        return availability()

    def open(self, operation_request):
        return self.session


class FakeVerifier:
    def __init__(self, accepted=True) -> None:
        self.accepted = accepted
        self.calls = 0

    def verify(self, operation_request, result, *, cancel):
        cancel.raise_if_cancelled()
        self.calls += 1
        return self.accepted


class OrchestratorTests(unittest.TestCase):
    def test_production_registry_has_zero_providers_and_two_unavailable_views(self) -> None:
        orchestrator = OperationOrchestrator.zero_provider(
            max_active_requests=2,
            max_retained_requests=8,
        )
        self.assertEqual(DEFAULT_PROVIDER_PORTS, ())
        self.assertEqual(orchestrator.provider_count, 0)
        self.assertEqual(len(orchestrator.availability_views()), 2)
        self.assertTrue(all(not item.available for item in orchestrator.availability_views()))
        refused = orchestrator.start(request())
        self.assertEqual(refused.status, OperationStatus.REFUSED)
        self.assertEqual(refused.error.code, OperationErrorCode.UNAVAILABLE)
        self.assertEqual(refused.emitted_commands, ())

        bounded = OperationOrchestrator.zero_provider(
            max_active_requests=1,
            max_retained_requests=1,
        )
        bounded.start(request())
        with self.assertRaises(OperationOrchestratorError):
            bounded.start(
                request(request_id="55555555-5555-4555-8555-555555555555")
            )
        bounded.forget(REQUEST_ID)
        self.assertEqual(
            bounded.start(
                request(request_id="55555555-5555-4555-8555-555555555555")
            ).status,
            OperationStatus.REFUSED,
        )

    def test_fake_provider_progress_result_verify_and_command_exit(self) -> None:
        session = FakeSession(
            (
                OperationProgress(REQUEST_ID, 1, ProgressStage.RUNNING, 10000),
                pixel_result(sequence=2),
            )
        )
        verifier = FakeVerifier()
        orchestrator = OperationOrchestrator(
            (FakeProvider(session),),
            verifier,
            max_active_requests=1,
            max_retained_requests=4,
        )
        self.assertEqual(orchestrator.start(request()).status, OperationStatus.SUBMITTED)
        self.assertEqual(
            orchestrator.poll(REQUEST_ID, cancel=CancelToken()).status,
            OperationStatus.ACTIVE,
        )
        ready = orchestrator.poll(REQUEST_ID, cancel=CancelToken())
        self.assertEqual(ready.status, OperationStatus.READY)
        self.assertEqual(verifier.calls, 1)
        self.assertTrue(session.closed)
        command = orchestrator.commit(REQUEST_ID, operation_command())
        self.assertIsInstance(command, ApplyOperationOutput)
        self.assertEqual(orchestrator.state(REQUEST_ID).emitted_commands, (command,))

    def test_fake_provider_crash_and_rejected_output_emit_zero_commands_and_no_prose(self) -> None:
        crash = OperationOrchestrator(
            (FakeProvider(FakeSession((), crash=True)),),
            FakeVerifier(),
            max_active_requests=1,
            max_retained_requests=4,
        )
        crash.start(request())
        failed = crash.poll(REQUEST_ID, cancel=CancelToken())
        self.assertEqual(failed.status, OperationStatus.REFUSED)
        self.assertEqual(failed.error.code, OperationErrorCode.PROVIDER_FAILURE)
        self.assertNotIn("private", render_operation_error(failed.error).text)
        self.assertEqual(failed.emitted_commands, ())

        refused_output = OperationOrchestrator(
            (FakeProvider(FakeSession((pixel_result(sequence=1),))),),
            FakeVerifier(False),
            max_active_requests=1,
            max_retained_requests=4,
        )
        refused_output.start(request())
        refused = refused_output.poll(REQUEST_ID, cancel=CancelToken())
        self.assertEqual(refused.status, OperationStatus.REFUSED)
        self.assertEqual(refused.error.code, OperationErrorCode.OUTPUT_INVALID)
        self.assertEqual(refused.emitted_commands, ())

    def test_provider_identity_mismatch_is_a_local_protocol_refusal(self) -> None:
        hostile = dataclasses.replace(
            pixel_result(sequence=1),
            provider_id="kilix.other-provider",
        )
        verifier = FakeVerifier()
        session = FakeSession((hostile,))
        orchestrator = OperationOrchestrator(
            (FakeProvider(session),),
            verifier,
            max_active_requests=1,
            max_retained_requests=4,
        )
        orchestrator.start(request())
        refused = orchestrator.poll(REQUEST_ID, cancel=CancelToken())
        self.assertEqual(refused.status, OperationStatus.REFUSED)
        self.assertEqual(refused.error.code, OperationErrorCode.PROTOCOL_ERROR)
        self.assertEqual(refused.emitted_commands, ())
        self.assertEqual(verifier.calls, 0)
        self.assertTrue(session.closed)

    def test_orchestrated_accepted_cancellation_waits_for_cancelled_terminal(self) -> None:
        outcome = CancellationOutcome(
            REQUEST_ID,
            CANCELLATION_ID,
            1,
            CancelDisposition.ACCEPTED,
            None,
        )
        session = FakeSession(
            (
                OperationError(
                    REQUEST_ID,
                    2,
                    OperationErrorCode.CANCELLED,
                    ErrorOrigin.PROVIDER,
                    False,
                ),
            ),
            cancel_outcome=outcome,
        )
        orchestrator = OperationOrchestrator(
            (FakeProvider(session),),
            FakeVerifier(),
            max_active_requests=1,
            max_retained_requests=4,
        )
        orchestrator.start(request())
        cancelling = orchestrator.cancel_request(
            REQUEST_ID,
            OperationCancel(CANCEL_SCHEMA, REQUEST_ID, CANCELLATION_ID),
        )
        self.assertEqual(cancelling.status, OperationStatus.CANCEL_ACCEPTED)
        cancelled = orchestrator.poll(REQUEST_ID, cancel=CancelToken())
        self.assertEqual(cancelled.status, OperationStatus.CANCELLED)
        self.assertEqual(cancelled.emitted_commands, ())


class DiagnosticAndDependencyTests(unittest.TestCase):
    def test_eight_error_codes_plus_unknown_render_only_fixed_local_text(self) -> None:
        self.assertEqual(len(diagnostic_catalogue()), 8)
        rendered = tuple(
            render_operation_error(
                OperationError(
                    REQUEST_ID,
                    0,
                    code,
                    ErrorOrigin.PROVIDER,
                    False,
                    "diag.fixture",
                )
            )
            for code in OperationErrorCode
        )
        self.assertEqual(len(rendered), 8)
        unknown = render_operation_error("hostile\x1b]8;;file:///private/path")
        self.assertEqual(unknown.message_id, "op.protocol_error")
        self.assertTrue(
            all(
                "private" not in item.text and "\x1b" not in item.text
                for item in rendered + (unknown,)
            )
        )

    def test_four_operation_modules_have_zero_provider_adapter_or_native_imports(self) -> None:
        root = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "kilix_image_shop"
            / "ops"
        )
        modules = tuple(
            sorted(path.name for path in root.glob("*.py") if path.name != "__init__.py")
        )
        self.assertEqual(
            modules,
            ("diagnostics.py", "messages.py", "orchestrator.py", "state.py"),
        )
        forbidden: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = (node.module,)
                else:
                    continue
                forbidden.extend(
                    name
                    for name in names
                    if name.startswith(("gi", "kilix_image_shop.ops.background_removal"))
                )
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
