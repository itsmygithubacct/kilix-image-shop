"""Command transaction boundary and toolkit-independent application use cases."""

from __future__ import annotations

from dataclasses import dataclass

from kilix_image_shop.domain.commands import (
    Command,
    CommandEffect,
    EffectKind,
    ReductionContext,
    ResolvedObject,
    reduce_command,
)
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import TextLayer
from kilix_image_shop.history.budget import HistoryUsage
from kilix_image_shop.history.stack import HistoryStack, HistoryTransition
from kilix_image_shop.ports import (
    EffectPort,
    ObjectPayload,
    ObjectPort,
    PortContractError,
    PresentationPort,
    ProjectPort,
)


class ApplicationError(RuntimeError):
    """An application transaction or use-case boundary is invalid."""


class ApplicationPostCommitError(ApplicationError):
    """A cache/presentation adapter failed after document publication."""

    def __init__(self, message: str, result: ApplicationResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    state: DocumentState
    effects: tuple[CommandEffect, ...]
    changed_layer_ids: tuple[LayerId, ...]
    history_usage: HistoryUsage

    def __post_init__(self) -> None:
        if not isinstance(self.state, DocumentState) or not isinstance(
            self.history_usage, HistoryUsage
        ):
            raise ApplicationError("application result lacks state or history usage")
        if not isinstance(self.effects, tuple) or any(
            not isinstance(item, CommandEffect) for item in self.effects
        ):
            raise ApplicationError("application effects must be immutable and typed")
        if not isinstance(self.changed_layer_ids, tuple) or any(
            not isinstance(item, LayerId) for item in self.changed_layer_ids
        ):
            raise ApplicationError("application changed layers must be immutable and typed")


@dataclass(frozen=True, slots=True)
class RestoreResult:
    transition: HistoryTransition
    history_usage: HistoryUsage

    def __post_init__(self) -> None:
        if not isinstance(self.transition, HistoryTransition) or not isinstance(
            self.history_usage, HistoryUsage
        ):
            raise ApplicationError("restore result is malformed")


def _referenced_object_ids(state: DocumentState) -> tuple[ObjectId, ...]:
    values: set[ObjectId] = {state.colour.working_profile}
    for asset in state.assets:
        values.update((asset.digest, asset.profile_digest))
    if state.selection is not None:
        values.add(state.selection.object_id)
    for layer in state.layers:
        mask = getattr(layer, "mask", None)
        if mask is not None:
            values.add(mask.object_id)
            if mask.source_ref is not None:
                values.add(mask.source_ref)
        if isinstance(layer, TextLayer):
            values.update((layer.font_digest, layer.preview_asset_digest))
            values.update(
                item.resolved_font_digest
                for item in layer.fallbacks
                if item.resolved_font_digest is not None
            )
    return tuple(sorted(values, key=lambda item: item.value))


class ApplicationService:
    """Publish one revision only after reduction and required object writes succeed."""

    def __init__(
        self,
        history: HistoryStack,
        objects: ObjectPort,
        effects: EffectPort,
        *,
        presentation: PresentationPort | None = None,
    ) -> None:
        if not isinstance(history, HistoryStack):
            raise ApplicationError("application requires a bounded history stack")
        if not isinstance(objects, ObjectPort) or not isinstance(effects, EffectPort):
            raise ApplicationError("application adapters do not implement their ports")
        if presentation is not None and not isinstance(presentation, PresentationPort):
            raise ApplicationError("presentation adapter does not implement its port")
        self._history = history
        self._objects = objects
        self._effects = effects
        self._presentation = presentation
        self._executing = False

    @property
    def state(self) -> DocumentState:
        return self._history.current_state

    @property
    def history_usage(self) -> HistoryUsage:
        return self._history.usage

    def _object_references(self, state: DocumentState) -> tuple[ResolvedObject, ...]:
        values: list[ResolvedObject] = []
        for object_id in _referenced_object_ids(state):
            try:
                reference = self._objects.resolve(object_id)
            except Exception as exc:
                raise PortContractError("document object identity cannot be resolved") from exc
            if not isinstance(reference, ResolvedObject) or reference.object_id != object_id:
                raise PortContractError("object resolver returned a different identity")
            try:
                verified = self._objects.verify(reference)
            except Exception as exc:
                raise PortContractError("document object verification failed") from exc
            if verified is not True:
                raise PortContractError("document object failed digest verification")
            values.append(reference)
        return tuple(values)

    @staticmethod
    def _payloads(values: tuple[ObjectPayload, ...]) -> dict[ObjectId, ObjectPayload]:
        if not isinstance(values, tuple) or any(
            not isinstance(item, ObjectPayload) for item in values
        ):
            raise ApplicationError("command payloads must be an immutable typed tuple")
        result = {item.reference.object_id: item for item in values}
        if len(result) != len(values):
            raise ApplicationError("command payloads contain a duplicate identity")
        return result

    def _present(self) -> None:
        if self._presentation is not None:
            self._presentation.document_changed(
                self.state,
                can_undo=self._history.can_undo,
                can_redo=self._history.can_redo,
            )

    def execute(
        self,
        command: Command,
        *,
        payloads: tuple[ObjectPayload, ...] = (),
    ) -> ApplicationResult:
        if self._executing:
            raise ApplicationError("application command execution is not reentrant")
        supplied = self._payloads(payloads)
        before = self.state
        context = ReductionContext(
            tuple(item.reference for item in supplied.values())
        )
        self._executing = True
        try:
            reduction = reduce_command(before, command, context)
            writes = tuple(
                effect
                for effect in reduction.effects
                if effect.kind is EffectKind.WRITE_OBJECT
            )
            required = {effect.object_id: effect for effect in writes}
            if len(required) != len(writes):
                raise ApplicationError("command emitted duplicate object-write effects")
            if set(required) != set(supplied):
                raise ApplicationError(
                    "command payload population differs from required object writes"
                )
            before_objects = self._object_references(before)
            for object_id in sorted(required, key=lambda item: item.value):
                value = supplied[object_id]
                effect = required[object_id]
                if effect.byte_count != value.reference.byte_count:
                    raise ApplicationError("command object effect differs from its payload")
                try:
                    self._objects.write(value)
                    verified = self._objects.verify(value.reference)
                except Exception as exc:
                    raise PortContractError("command object publication failed") from exc
                if verified is not True:
                    raise PortContractError("written command object failed verification")
            after_objects = self._object_references(reduction.state)
            usage = self._history.record(
                command,
                reduction,
                before_objects=before_objects,
                after_objects=after_objects,
            )
            result = ApplicationResult(
                reduction.state,
                reduction.effects,
                reduction.changed_layer_ids,
                usage,
            )
            non_write = tuple(
                effect
                for effect in result.effects
                if effect.kind is not EffectKind.WRITE_OBJECT
            )
            try:
                self._effects.publish(before, reduction, non_write)
                self._present()
            except Exception as exc:
                raise ApplicationPostCommitError(
                    "document committed but a post-commit adapter failed",
                    result,
                ) from exc
            return result
        finally:
            self._executing = False

    def _restore(
        self,
        direction: str,
        *,
        expected_revision: RevisionId,
        new_revision: RevisionId,
    ) -> RestoreResult:
        if self._executing:
            raise ApplicationError("application use cases are not reentrant")
        self._executing = True
        try:
            before = self.state
            method = self._history.undo if direction == "undo" else self._history.redo
            transition = method(
                expected_revision=expected_revision,
                new_revision=new_revision,
                object_validator=self._objects.verify,
            )
            result = RestoreResult(transition, self._history.usage)
            try:
                self._effects.restore(
                    transition.state,
                    transition.invalidated_layer_ids,
                )
                self._present()
            except Exception as exc:
                application_result = ApplicationResult(
                    transition.state,
                    (),
                    transition.invalidated_layer_ids,
                    self._history.usage,
                )
                raise ApplicationPostCommitError(
                    f"{direction} committed but a post-commit adapter failed",
                    application_result,
                ) from exc
            if before.revision_id == transition.state.revision_id:
                raise ApplicationError("history restore failed to advance the revision")
            return result
        finally:
            self._executing = False

    def undo(
        self,
        *,
        expected_revision: RevisionId,
        new_revision: RevisionId,
    ) -> RestoreResult:
        return self._restore(
            "undo",
            expected_revision=expected_revision,
            new_revision=new_revision,
        )

    def redo(
        self,
        *,
        expected_revision: RevisionId,
        new_revision: RevisionId,
    ) -> RestoreResult:
        return self._restore(
            "redo",
            expected_revision=expected_revision,
            new_revision=new_revision,
        )

    def save(self, project: ProjectPort) -> ObjectId:
        if not isinstance(project, ProjectPort):
            raise ApplicationError("project adapter does not implement its port")
        if self._executing:
            raise ApplicationError("application use cases are not reentrant")
        self._executing = True
        try:
            captured = self.state
            try:
                generation = project.save(captured)
            except Exception as exc:
                raise PortContractError("project save adapter failed") from exc
            if not isinstance(generation, ObjectId):
                raise PortContractError("project adapter returned an untyped generation")
            return generation
        finally:
            self._executing = False


__all__ = (
    "ApplicationError",
    "ApplicationPostCommitError",
    "ApplicationResult",
    "ApplicationService",
    "RestoreResult",
)
