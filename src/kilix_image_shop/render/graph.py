"""Revision-keyed graph dependencies and pure invalidation decisions."""

from __future__ import annotations

from dataclasses import dataclass

from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.engine.api import InvalidGraph


def _sorted_unique(values: tuple[object, ...], expected: type[object], label: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(item, expected) for item in values
    ):
        raise InvalidGraph(f"{label} must be an immutable typed tuple")
    identities = tuple(item.value for item in values)
    if identities != tuple(sorted(set(identities))):
        raise InvalidGraph(f"{label} must be sorted and unique")


def _rect_key(rectangle: Rect) -> tuple[int, int, int, int]:
    return (rectangle.y, rectangle.x, rectangle.height, rectangle.width)


@dataclass(frozen=True, slots=True)
class GraphDependency:
    """Everything whose change makes one compiled graph identity stale."""

    graph_digest: ObjectId
    revision: RevisionId
    layers: tuple[LayerId, ...]
    objects: tuple[ObjectId, ...]
    output_bounds: Rect

    def __post_init__(self) -> None:
        if not isinstance(self.graph_digest, ObjectId):
            raise InvalidGraph("graph dependency requires a graph digest")
        if not isinstance(self.revision, RevisionId):
            raise InvalidGraph("graph dependency requires a document revision")
        _sorted_unique(self.layers, LayerId, "graph layer dependencies")
        _sorted_unique(self.objects, ObjectId, "graph object dependencies")
        if not self.layers and not self.objects:
            raise InvalidGraph("graph dependency cannot be empty")
        if not isinstance(self.output_bounds, Rect):
            raise InvalidGraph("graph dependency requires checked output bounds")


@dataclass(frozen=True, slots=True)
class InvalidationRequest:
    source_revision: RevisionId
    replacement_revision: RevisionId
    changed_layers: tuple[LayerId, ...]
    changed_objects: tuple[ObjectId, ...]
    affected_rectangles: tuple[Rect, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_revision, RevisionId) or not isinstance(
            self.replacement_revision, RevisionId
        ):
            raise InvalidGraph("invalidation requires typed revisions")
        if self.source_revision == self.replacement_revision:
            raise InvalidGraph("invalidation must advance the document revision")
        _sorted_unique(self.changed_layers, LayerId, "changed layers")
        _sorted_unique(self.changed_objects, ObjectId, "changed objects")
        if not self.changed_layers and not self.changed_objects:
            raise InvalidGraph("invalidation requires at least one changed dependency")
        if not isinstance(self.affected_rectangles, tuple) or not self.affected_rectangles:
            raise InvalidGraph("invalidation requires affected rectangles")
        if any(not isinstance(item, Rect) for item in self.affected_rectangles):
            raise InvalidGraph("affected rectangles must be checked geometry")
        keys = tuple(_rect_key(item) for item in self.affected_rectangles)
        if keys != tuple(sorted(set(keys))):
            raise InvalidGraph("affected rectangles must be sorted and unique")


@dataclass(frozen=True, slots=True)
class InvalidationResult:
    source_revision: RevisionId
    replacement_revision: RevisionId
    graph_digests: tuple[ObjectId, ...]
    affected_rectangles: tuple[Rect, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_revision, RevisionId) or not isinstance(
            self.replacement_revision, RevisionId
        ):
            raise InvalidGraph("invalidation result requires typed revisions")
        _sorted_unique(self.graph_digests, ObjectId, "invalidated graph digests")
        if not isinstance(self.affected_rectangles, tuple) or any(
            not isinstance(item, Rect) for item in self.affected_rectangles
        ):
            raise InvalidGraph("invalidation result rectangles are malformed")


def plan_invalidation(
    dependencies: tuple[GraphDependency, ...],
    request: InvalidationRequest,
) -> InvalidationResult:
    """Select every source-revision graph depending on a changed layer/object."""

    if not isinstance(dependencies, tuple) or any(
        not isinstance(item, GraphDependency) for item in dependencies
    ):
        raise InvalidGraph("graph dependencies must be an immutable typed tuple")
    digests = tuple(item.graph_digest.value for item in dependencies)
    if len(digests) != len(set(digests)):
        raise InvalidGraph("graph dependency index repeats a graph digest")
    if not isinstance(request, InvalidationRequest):
        raise InvalidGraph("invalidation request is untyped")
    changed_layers = set(request.changed_layers)
    changed_objects = set(request.changed_objects)
    selected = tuple(
        sorted(
            (
                item.graph_digest
                for item in dependencies
                if item.revision == request.source_revision
                and (
                    bool(changed_layers.intersection(item.layers))
                    or bool(changed_objects.intersection(item.objects))
                )
            ),
            key=lambda item: item.value,
        )
    )
    return InvalidationResult(
        request.source_revision,
        request.replacement_revision,
        selected,
        request.affected_rectangles,
    )


__all__ = (
    "GraphDependency",
    "InvalidationRequest",
    "InvalidationResult",
    "plan_invalidation",
)
