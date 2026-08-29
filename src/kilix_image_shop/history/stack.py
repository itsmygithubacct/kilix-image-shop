"""Bounded undo/redo cursor over reversible immutable document records."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from kilix_image_shop.domain.commands import (
    COMMAND_TYPES,
    Command,
    ReductionResult,
    ResolvedObject,
)
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import (
    DocumentId,
    LayerId,
    ObjectId,
    RevisionId,
)

from .budget import HistoryBudget, HistoryUsage
from .spill import SpillError, SpillRef, SpillStore


HISTORY_RECORD_SCHEMA = "kilix.imageshop.history-record/v1"
RESTORE_CONTROLS = (
    "expected-revision",
    "object-digest-validation",
    "proxy-invalidation",
    "atomic-publication",
)
ObjectValidator = Callable[[ResolvedObject], bool]


class HistoryError(RuntimeError):
    """A history transition is invalid or unavailable."""


class UndoUnavailable(HistoryError):
    """The retained undo horizon contains no earlier entry."""


class RedoUnavailable(HistoryError):
    """The retained cursor contains no redo entry."""


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
        raise HistoryError("history record cannot be serialized canonically") from exc


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HistoryError(f"duplicate history JSON member: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise HistoryError(f"non-finite history JSON number is forbidden: {value}")


def _parse(payload: bytes) -> object:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryError("history record is not strict UTF-8 JSON") from exc
    if _canonical(value) != payload:
        raise HistoryError("history record is not in canonical form")
    return value


def _command_value(value: object) -> object:
    if isinstance(value, (DocumentId, RevisionId, LayerId, ObjectId)):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _command_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_command_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise HistoryError("command contains a non-serializable history value")


def command_bytes(command: Command) -> bytes:
    if type(command) not in COMMAND_TYPES:
        raise HistoryError("history command type is outside the closed population")
    return _canonical({"type": type(command).__name__, "value": _command_value(command)})


def _object_data(reference: ResolvedObject) -> dict[str, object]:
    return {"byteCount": reference.byte_count, "sha256": reference.object_id.value}


def _objects_from_data(value: object) -> tuple[ResolvedObject, ...]:
    if not isinstance(value, list):
        raise HistoryError("history object references must be a list")
    result: list[ResolvedObject] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"byteCount", "sha256"}:
            raise HistoryError("history object reference is malformed")
        try:
            result.append(
                ResolvedObject(ObjectId.parse(item["sha256"]), item["byteCount"])
            )
        except ValueError as exc:
            raise HistoryError("history object reference is invalid") from exc
    return tuple(result)


def _normalize_objects(
    values: tuple[ResolvedObject, ...], field: str
) -> tuple[ResolvedObject, ...]:
    if not isinstance(values, tuple) or any(
        not isinstance(item, ResolvedObject) for item in values
    ):
        raise HistoryError(f"{field} must be an immutable object-reference tuple")
    normalized = tuple(sorted(values, key=lambda item: item.object_id.value))
    if len({item.object_id for item in normalized}) != len(normalized):
        raise HistoryError(f"{field} contains duplicate object identities")
    return normalized


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    command_payload: bytes
    before: DocumentState
    after: DocumentState
    before_objects: tuple[ResolvedObject, ...]
    after_objects: tuple[ResolvedObject, ...]
    changed_layer_ids: tuple[LayerId, ...]

    def __post_init__(self) -> None:
        command = _parse(self.command_payload)
        if not isinstance(command, dict) or set(command) != {"type", "value"}:
            raise HistoryError("history command carrier is malformed")
        if command["type"] not in {item.__name__ for item in COMMAND_TYPES} or not isinstance(
            command["value"], dict
        ):
            raise HistoryError("history command carrier names an unsupported command")
        if not isinstance(self.before, DocumentState) or not isinstance(
            self.after, DocumentState
        ):
            raise HistoryError("history record requires typed document states")
        if self.before.document_id != self.after.document_id:
            raise HistoryError("history record crosses document identities")
        if self.before.revision_id == self.after.revision_id:
            raise HistoryError("history record must advance the revision")
        object.__setattr__(
            self,
            "before_objects",
            _normalize_objects(self.before_objects, "before object references"),
        )
        object.__setattr__(
            self,
            "after_objects",
            _normalize_objects(self.after_objects, "after object references"),
        )
        if not isinstance(self.changed_layer_ids, tuple) or any(
            not isinstance(item, LayerId) for item in self.changed_layer_ids
        ):
            raise HistoryError("changed layer IDs must be an immutable typed tuple")
        normalized_layers = tuple(
            sorted(set(self.changed_layer_ids), key=lambda item: item.value)
        )
        object.__setattr__(self, "changed_layer_ids", normalized_layers)

    def to_data(self) -> dict[str, object]:
        return {
            "after": self.after.to_manifest(),
            "afterObjects": [_object_data(item) for item in self.after_objects],
            "before": self.before.to_manifest(),
            "beforeObjects": [_object_data(item) for item in self.before_objects],
            "changedLayerIds": [item.value for item in self.changed_layer_ids],
            "command": _parse(self.command_payload),
            "schema": HISTORY_RECORD_SCHEMA,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_data())

    @property
    def entry_id(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())

    @property
    def resident_bytes(self) -> int:
        return len(self.canonical_bytes())

    @classmethod
    def from_bytes(cls, payload: bytes) -> HistoryRecord:
        value = _parse(payload)
        required = {
            "after",
            "afterObjects",
            "before",
            "beforeObjects",
            "changedLayerIds",
            "command",
            "schema",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise HistoryError("history record has missing or unknown fields")
        if value["schema"] != HISTORY_RECORD_SCHEMA:
            raise HistoryError("history record schema is unsupported")
        changed = value["changedLayerIds"]
        if not isinstance(changed, list):
            raise HistoryError("history changed-layer table is malformed")
        try:
            return cls(
                _canonical(value["command"]),
                DocumentState.from_manifest(value["before"]),
                DocumentState.from_manifest(value["after"]),
                _objects_from_data(value["beforeObjects"]),
                _objects_from_data(value["afterObjects"]),
                tuple(LayerId.parse(item) for item in changed),
            )
        except ValueError as exc:
            raise HistoryError("history record contains invalid domain values") from exc


@dataclass(frozen=True, slots=True)
class HistoryTransition:
    direction: str
    state: DocumentState
    invalidated_layer_ids: tuple[LayerId, ...]
    validated_objects: tuple[ResolvedObject, ...]
    controls: tuple[str, ...] = RESTORE_CONTROLS

    def __post_init__(self) -> None:
        if self.direction not in {"undo", "redo"}:
            raise HistoryError("history transition direction is not closed")
        if self.controls != RESTORE_CONTROLS:
            raise HistoryError("history transition lacks all four restore controls")


Entry = HistoryRecord | SpillRef


class HistoryStack:
    """Own the current revision and publish restore transitions atomically."""

    def __init__(
        self,
        initial_state: DocumentState,
        budget: HistoryBudget,
        spill: SpillStore,
    ) -> None:
        if not isinstance(initial_state, DocumentState):
            raise HistoryError("history requires an initial document state")
        if not isinstance(budget, HistoryBudget) or not isinstance(spill, SpillStore):
            raise HistoryError("history requires typed budget and spill store")
        if spill.max_record_bytes < budget.max_spill_bytes:
            raise HistoryError("spill record budget cannot be below total spill budget")
        self._state = initial_state
        self._budget = budget
        self._spill = spill
        self._entries: list[Entry] = []
        self._cursor = 0
        self._pruned_entries = 0
        self._used_revisions = {initial_state.revision_id}

    @property
    def current_state(self) -> DocumentState:
        return self._state

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor < len(self._entries)

    @property
    def usage(self) -> HistoryUsage:
        return HistoryUsage(
            entries=len(self._entries),
            undoable_entries=self._cursor,
            redoable_entries=len(self._entries) - self._cursor,
            resident_bytes=sum(
                item.resident_bytes
                for item in self._entries
                if isinstance(item, HistoryRecord)
            ),
            spill_bytes=sum(
                item.byte_count for item in self._entries if isinstance(item, SpillRef)
            ),
            pruned_entries=self._pruned_entries,
            budget=self._budget,
        )

    def _delete_entry(self, entry: Entry) -> None:
        if isinstance(entry, SpillRef):
            self._spill.delete(entry)

    def _discard_redo(self) -> None:
        for entry in self._entries[self._cursor :]:
            self._delete_entry(entry)
        del self._entries[self._cursor :]

    def _prune_oldest(self) -> None:
        if not self._entries:
            return
        entry = self._entries.pop(0)
        was_undoable = self._cursor > 0
        if was_undoable:
            self._cursor -= 1
            self._pruned_entries += 1
        self._delete_entry(entry)

    def _resident_bytes(self) -> int:
        return sum(
            item.resident_bytes
            for item in self._entries
            if isinstance(item, HistoryRecord)
        )

    def _spill_bytes(self) -> int:
        return sum(item.byte_count for item in self._entries if isinstance(item, SpillRef))

    def _enforce_budget(self) -> None:
        while len(self._entries) > self._budget.max_entries:
            self._prune_oldest()
        while self._resident_bytes() > self._budget.max_resident_bytes:
            candidate = next(
                (
                    (index, entry)
                    for index, entry in enumerate(self._entries[: self._cursor])
                    if isinstance(entry, HistoryRecord)
                ),
                None,
            )
            if candidate is None:
                raise HistoryError("resident history cannot be reduced below its budget")
            index, record = candidate
            payload = record.canonical_bytes()
            if len(payload) > self._budget.max_spill_bytes:
                self._prune_oldest()
                continue
            while self._spill_bytes() + len(payload) > self._budget.max_spill_bytes:
                self._prune_oldest()
                if index == 0:
                    break
                index -= 1
            if record not in self._entries:
                continue
            index = self._entries.index(record)
            self._entries[index] = self._spill.put(record.entry_id, payload)

    def record(
        self,
        command: Command,
        result: ReductionResult,
        *,
        before_objects: tuple[ResolvedObject, ...] = (),
        after_objects: tuple[ResolvedObject, ...] = (),
    ) -> HistoryUsage:
        """Retain one completed reduction and publish its document revision."""

        if not isinstance(result, ReductionResult):
            raise HistoryError("history append requires a typed reduction result")
        if result.before_revision != self._state.revision_id:
            raise HistoryError("history reduction does not start at the current revision")
        if (
            command.expected_revision != self._state.revision_id
            or command.new_revision != result.state.revision_id
        ):
            raise HistoryError("history command and reduction revisions do not join")
        if result.state.document_id != self._state.document_id:
            raise HistoryError("history reduction crosses document identities")
        if result.state.revision_id in self._used_revisions:
            raise HistoryError("document revision identifiers cannot be reused")
        record = HistoryRecord(
            command_bytes(command),
            self._state,
            result.state,
            before_objects,
            after_objects,
            result.changed_layer_ids,
        )
        self._discard_redo()
        self._entries.append(record)
        self._cursor += 1
        self._enforce_budget()
        self._state = result.state
        self._used_revisions.add(result.state.revision_id)
        return self.usage

    def _materialize(self, entry: Entry) -> HistoryRecord:
        if isinstance(entry, HistoryRecord):
            return entry
        try:
            payload = self._spill.get(entry)
            record = HistoryRecord.from_bytes(payload)
        except (HistoryError, SpillError) as exc:
            raise HistoryError("spilled history record cannot be restored") from exc
        if record.entry_id != entry.entry_id:
            raise HistoryError("spilled history record identity differs")
        return record

    @staticmethod
    def _same_content(current: DocumentState, template: DocumentState) -> bool:
        return replace(current, revision_id=template.revision_id) == template

    def _validate_restore(
        self,
        expected_revision: RevisionId,
        new_revision: RevisionId,
        references: tuple[ResolvedObject, ...],
        validator: ObjectValidator,
    ) -> None:
        if not isinstance(expected_revision, RevisionId) or not isinstance(
            new_revision, RevisionId
        ):
            raise HistoryError("history restore revisions must be typed")
        if expected_revision != self._state.revision_id:
            raise HistoryError("history restore expected revision is stale")
        if new_revision in self._used_revisions:
            raise HistoryError("document revision identifiers cannot be reused")
        if not callable(validator):
            raise HistoryError("history object validator is not callable")
        for reference in references:
            try:
                valid = validator(reference)
            except Exception as exc:
                raise HistoryError("history object digest validation failed") from exc
            if valid is not True:
                raise HistoryError("history object digest validation failed")

    def undo(
        self,
        *,
        expected_revision: RevisionId,
        new_revision: RevisionId,
        object_validator: ObjectValidator,
    ) -> HistoryTransition:
        if not self.can_undo:
            raise UndoUnavailable("undo horizon has no retained entry")
        record = self._materialize(self._entries[self._cursor - 1])
        if not self._same_content(self._state, record.after):
            raise HistoryError("current document content diverged from undo history")
        self._validate_restore(
            expected_revision,
            new_revision,
            record.before_objects,
            object_validator,
        )
        candidate = replace(record.before, revision_id=new_revision)
        transition = HistoryTransition(
            "undo",
            candidate,
            record.changed_layer_ids,
            record.before_objects,
        )
        self._cursor -= 1
        self._state = candidate
        self._used_revisions.add(new_revision)
        return transition

    def redo(
        self,
        *,
        expected_revision: RevisionId,
        new_revision: RevisionId,
        object_validator: ObjectValidator,
    ) -> HistoryTransition:
        if not self.can_redo:
            raise RedoUnavailable("redo cursor has no retained entry")
        record = self._materialize(self._entries[self._cursor])
        if not self._same_content(self._state, record.before):
            raise HistoryError("current document content diverged from redo history")
        self._validate_restore(
            expected_revision,
            new_revision,
            record.after_objects,
            object_validator,
        )
        candidate = replace(record.after, revision_id=new_revision)
        transition = HistoryTransition(
            "redo",
            candidate,
            record.changed_layer_ids,
            record.after_objects,
        )
        self._cursor += 1
        self._state = candidate
        self._used_revisions.add(new_revision)
        return transition

    def reachable_object_ids(self) -> tuple[ObjectId, ...]:
        """Return retained undo and redo refs for project/cache reachability GC."""

        values: set[ObjectId] = set()
        for entry in self._entries:
            record = self._materialize(entry)
            values.update(item.object_id for item in record.before_objects)
            values.update(item.object_id for item in record.after_objects)
        return tuple(sorted(values, key=lambda item: item.value))
