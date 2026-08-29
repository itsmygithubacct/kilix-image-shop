"""Guarded process-scoped GEGL/babl lifecycle.

This is the sole module allowed to import GI.  Imports occur inside the guarded
loader only after deterministic environment, carrier, and private-path checks.
"""

from __future__ import annotations

import fcntl
import gc
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Protocol, TypeAlias

from kilix_image_shop.domain.color import EngineCompatibility
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId

from .api import (
    AdjustmentParameters,
    AffineTransformCropParameters,
    BufferInventory,
    BufferInventoryEntry,
    BufferRef,
    CancelToken,
    ColourConversionParameters,
    DecodeRefusal,
    DestinationCropScaleParameters,
    EngineCapabilities,
    EngineDiagnostics,
    EngineFailure,
    GraphNodeSpec,
    GraphNodeKind,
    GraphSpec,
    IncompatibleRuntime,
    InternalEngineFailure,
    InvalidGraph,
    MaskTileDigest,
    MaskTileUpdate,
    MaskParameters,
    OpacityBlendParameters,
    OrderedGroupParameters,
    PixelFormat,
    PixelSourceParameters,
    PixelSpec,
    ProcessMemoryDiagnostics,
    ProxyDiagnostics,
    QueueDiagnostics,
    ResourceExhaustion,
    TextSourceParameters,
    TileRequest,
    TileResult,
    SwapDiagnostics,
    TimingDiagnostics,
    UnavailableGroup,
    mask_digest_index,
    mask_manifest_digest,
)
from .compatibility import (
    GI_ORIGIN,
    H0_TILE_CACHE_BYTES,
    NativeObservation,
    OperationDefinition,
    OperationProperty,
    OperationRegistry,
    PropertySource,
    RegistryValueKind,
    RuntimeConfiguration,
    validate_native_observation,
)
from .formats import RenderTier, TierFormatPolicy


class StartupStep(StrEnum):
    VALIDATE_CONFIGURATION = "validate-configuration"
    VALIDATE_ENVIRONMENT = "validate-environment"
    CREATE_PRIVATE_PATHS = "create-private-paths"
    LOAD_GI_NAMESPACES = "load-gi-namespaces"
    INITIALIZE_GEGL = "initialize-gegl"
    APPLY_CONFIGURATION = "apply-configuration"
    READ_BACK_CONFIGURATION = "read-back-configuration"
    VERIFY_COMPATIBILITY = "verify-compatibility"
    RUN_SMOKE_GRAPH = "run-smoke-graph"
    PUBLISH_HANDLE = "publish-handle"


STARTUP_SEQUENCE: tuple[StartupStep, ...] = tuple(StartupStep)


class NativeRuntimeBackend(Protocol):
    def initialize(self) -> None: ...

    def configure(self, values: tuple[tuple[str, object], ...]) -> None: ...

    def read_configuration(self, names: tuple[str, ...]) -> dict[str, object]: ...

    def observe(self) -> NativeObservation: ...

    def verify_operation_registry(self, registry: OperationRegistry) -> None: ...

    def validate_profile(self, path: pathlib.Path, encoding: str) -> None: ...

    def import_pixels(
        self,
        payload: bytes,
        extent: Rect,
        encoding: str,
    ) -> object: ...

    def compile_plan(
        self,
        plan: CompiledGraphPlan,
        buffers: dict[str, object],
        profiles: dict[ObjectId, pathlib.Path],
    ) -> object: ...

    def build_proxy(
        self,
        graph: object,
        *,
        level: int,
        expected_extent: Rect,
        encoding: str,
        scale_definition: OperationDefinition,
        sink_definition: OperationDefinition,
    ) -> object: ...

    def read_buffer(
        self,
        buffer: object,
        rectangle: Rect,
        encoding: str,
    ) -> bytes: ...

    def render_tile(
        self,
        source: object,
        *,
        source_rectangle: Rect,
        destination_rectangle: Rect,
        encoding: str,
        source_definition: OperationDefinition,
        crop_definition: OperationDefinition,
        scale_definition: OperationDefinition,
    ) -> bytes: ...

    def duplicate_buffer(self, buffer: object) -> object: ...

    def write_buffer(
        self,
        buffer: object,
        rectangle: Rect,
        encoding: str,
        payload: bytes,
    ) -> None: ...

    def release_buffer(self, buffer: object) -> None: ...

    def release_graph(self, graph: object) -> None: ...

    def smoke_test(self) -> None: ...

    def shutdown(self) -> None: ...


class _GuardState(StrEnum):
    PRISTINE = "pristine"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    CLOSED = "closed"


class RuntimeProcessGuard:
    """One-way process lifecycle; the product uses the module-global instance."""

    def __init__(self) -> None:
        self._state = _GuardState.PRISTINE
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state.value

    def begin(self) -> None:
        with self._lock:
            if self._state is not _GuardState.PRISTINE:
                raise IncompatibleRuntime("GEGL runtime can initialize only once per process")
            self._state = _GuardState.STARTING

    def publish(self) -> None:
        with self._lock:
            if self._state is not _GuardState.STARTING:
                raise InternalEngineFailure("runtime process guard lost startup state")
            self._state = _GuardState.RUNNING

    def fail(self) -> None:
        with self._lock:
            self._state = _GuardState.FAILED

    def close(self) -> None:
        with self._lock:
            if self._state is not _GuardState.RUNNING:
                raise InternalEngineFailure("runtime process guard is not running")
            self._state = _GuardState.CLOSED


_GLOBAL_PROCESS_GUARD = RuntimeProcessGuard()


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    """Published only after all startup checks; contains no GI/native object."""

    compatibility: EngineCompatibility
    capabilities: EngineCapabilities
    swap_directory: pathlib.Path
    completed_steps: tuple[StartupStep, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.compatibility, EngineCompatibility):
            raise InternalEngineFailure("runtime handle lacks compatibility identity")
        if not isinstance(self.capabilities, EngineCapabilities):
            raise InternalEngineFailure("runtime handle lacks typed capabilities")
        if not isinstance(
            self.swap_directory, pathlib.Path
        ) or not self.swap_directory.is_absolute():
            raise InternalEngineFailure("runtime handle swap path is not absolute")
        if self.completed_steps != STARTUP_SEQUENCE:
            raise InternalEngineFailure("runtime handle was published before startup completed")
        if self.capabilities.compatibility_digest != self.compatibility.digest:
            raise InternalEngineFailure("runtime capability identity differs from compatibility")

    @property
    def compatibility_digest(self) -> ObjectId:
        return self.compatibility.digest


@dataclass(frozen=True, slots=True)
class BufferBinding:
    token: str


@dataclass(frozen=True, slots=True)
class ProfileBinding:
    digest: ObjectId


BoundPropertyValue: TypeAlias = (
    bool
    | int
    | float
    | str
    | tuple[float, ...]
    | BufferBinding
    | ProfileBinding
)


@dataclass(frozen=True, slots=True)
class BoundProperty:
    native_name: str
    value: BoundPropertyValue


@dataclass(frozen=True, slots=True)
class PlanInput:
    source_plan_id: str
    source_pad: str
    destination_pad: str


@dataclass(frozen=True, slots=True)
class CompiledPlanNode:
    plan_id: str
    semantic_key: str
    native_operation: str
    properties: tuple[BoundProperty, ...]
    inputs: tuple[PlanInput, ...]
    output_pad: str | None


@dataclass(frozen=True, slots=True)
class CompiledGraphPlan:
    graph_digest: ObjectId
    revision: RevisionId
    nodes: tuple[CompiledPlanNode, ...]
    output_plan_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.graph_digest, ObjectId):
            raise InvalidGraph("compiled graph plan lacks a graph digest")
        if not isinstance(self.revision, RevisionId):
            raise InvalidGraph("compiled graph plan lacks a document revision")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise InvalidGraph("compiled graph plan must contain native nodes")
        identifiers = tuple(item.plan_id for item in self.nodes)
        if len(identifiers) != len(set(identifiers)):
            raise InvalidGraph("compiled graph plan repeats a native node ID")
        if self.output_plan_id not in identifiers:
            raise InvalidGraph("compiled graph plan output node is missing")


def _binding_value(
    specification: OperationProperty,
    value: object,
) -> BoundPropertyValue:
    kind = specification.value_kind
    if kind is RegistryValueKind.BOOLEAN:
        if not isinstance(value, bool):
            raise InvalidGraph("registry boolean property received the wrong type")
        return value
    if kind is RegistryValueKind.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidGraph("registry integer property received the wrong type")
        return value
    if kind is RegistryValueKind.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidGraph("registry number property received the wrong type")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise InvalidGraph("registry number property must be finite")
        return 0.0 if parsed == 0.0 else parsed
    if kind is RegistryValueKind.NUMBER_VECTOR:
        if not isinstance(value, tuple) or not value:
            raise InvalidGraph("registry vector property requires a non-empty tuple")
        parsed_values: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise InvalidGraph("registry vector property contains a non-number")
            parsed = float(item)
            if not math.isfinite(parsed):
                raise InvalidGraph("registry vector property must be finite")
            parsed_values.append(0.0 if parsed == 0.0 else parsed)
        return tuple(parsed_values)
    if kind is RegistryValueKind.STRING:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise InvalidGraph("registry string property received the wrong type")
        return value
    if kind is RegistryValueKind.BUFFER:
        if not isinstance(value, BufferRef):
            raise InvalidGraph("registry buffer property requires an opaque buffer ref")
        return BufferBinding(value.token)
    if kind is RegistryValueKind.PROFILE_PATH:
        if not isinstance(value, ObjectId):
            raise InvalidGraph("registry profile property requires a profile digest")
        return ProfileBinding(value)
    raise InvalidGraph("registry value kind is unsupported")


def _bind_properties(
    definition: OperationDefinition,
    semantic_values: dict[str, object],
) -> tuple[BoundProperty, ...]:
    expected_semantics = {
        item.semantic_name
        for item in definition.properties
        if item.source is PropertySource.SEMANTIC
    }
    if set(semantic_values) != expected_semantics:
        raise InvalidGraph("semantic properties differ from the accepted operation map")
    bound: list[BoundProperty] = []
    for item in definition.properties:
        if item.source is PropertySource.DEFAULT:
            continue
        value = (
            item.fixed_value
            if item.source is PropertySource.FIXED
            else semantic_values[item.semantic_name]
        )
        bound.append(BoundProperty(item.native_name, _binding_value(item, value)))
    return tuple(bound)


def _affine_string(parameters: AffineTransformCropParameters) -> str:
    transform = parameters.transform
    values = (transform.a, transform.b, transform.c, transform.d, transform.e, transform.f)
    return "matrix(" + ",".join(format(value, ".17g") for value in values) + ")"


class _GraphPlanCompiler:
    def __init__(self, registry: OperationRegistry) -> None:
        self._registry = registry
        self._nodes: list[CompiledPlanNode] = []
        self._by_id: dict[str, CompiledPlanNode] = {}

    def _add(
        self,
        plan_id: str,
        semantic_key: str,
        semantic_values: dict[str, object],
        inputs: tuple[tuple[str, str], ...],
    ) -> str:
        if plan_id in self._by_id:
            raise InvalidGraph("native compiler generated a duplicate plan node")
        definition = self._registry.definition(semantic_key)
        if tuple(destination for _, destination in inputs) != definition.input_pads:
            raise InvalidGraph("compiler input pads differ from the accepted operation map")
        planned_inputs: list[PlanInput] = []
        for source_id, destination_pad in inputs:
            source = self._by_id.get(source_id)
            if source is None or source.output_pad is None:
                raise InvalidGraph("compiler input refers to an unavailable native output")
            planned_inputs.append(
                PlanInput(source_id, source.output_pad, destination_pad)
            )
        node = CompiledPlanNode(
            plan_id=plan_id,
            semantic_key=semantic_key,
            native_operation=definition.operation,
            properties=_bind_properties(definition, semantic_values),
            inputs=tuple(planned_inputs),
            output_pad=definition.output_pad,
        )
        self._nodes.append(node)
        self._by_id[plan_id] = node
        return plan_id

    def _halo(self, node: GraphNodeSpec, *semantic_keys: str) -> None:
        required = max(
            (self._registry.definition(key).halo_pixels for key in semantic_keys),
            default=0,
        )
        if node.halo_pixels != required:
            raise InvalidGraph("graph node halo differs from the accepted operation map")

    def compile(
        self,
        graph: GraphSpec,
        buffers: tuple[BufferRef, ...],
        profiles: tuple[ObjectId, ...],
    ) -> CompiledGraphPlan:
        if not isinstance(graph, GraphSpec):
            raise InvalidGraph("native compiler requires a closed graph spec")
        if not isinstance(buffers, tuple) or any(
            not isinstance(item, BufferRef) for item in buffers
        ):
            raise InvalidGraph("native compiler requires immutable buffer refs")
        if not isinstance(profiles, tuple) or any(
            not isinstance(item, ObjectId) for item in profiles
        ):
            raise InvalidGraph("native compiler requires immutable profile identities")
        if len(set(profiles)) != len(profiles):
            raise InvalidGraph("native compiler profile identities must be unique")

        graph_outputs: dict[str, str] = {}
        for node in graph.nodes:
            parameters = node.parameters
            if node.kind in {GraphNodeKind.PIXEL_SOURCE, GraphNodeKind.TEXT_SOURCE}:
                key = (
                    "source.pixel"
                    if node.kind is GraphNodeKind.PIXEL_SOURCE
                    else "source.text-raster"
                )
                self._halo(node, key)
                buffer = self._resolve_source(node, graph.revision, buffers)
                graph_outputs[node.node_id] = self._add(
                    f"{node.node_id}__source",
                    key,
                    {"buffer": buffer},
                    (),
                )
                continue

            inputs = tuple(graph_outputs[item] for item in node.inputs)
            if isinstance(parameters, AffineTransformCropParameters):
                self._halo(node, "transform.affine", "transform.crop")
                transformed = self._add(
                    f"{node.node_id}__affine",
                    "transform.affine",
                    {"transform": _affine_string(parameters)},
                    ((inputs[0], "input"),),
                )
                crop = parameters.crop
                graph_outputs[node.node_id] = self._add(
                    f"{node.node_id}__crop",
                    "transform.crop",
                    {
                        "x": crop.x,
                        "y": crop.y,
                        "width": crop.width,
                        "height": crop.height,
                    },
                    ((transformed, "input"),),
                )
            elif isinstance(parameters, OpacityBlendParameters):
                mode_key = f"blend.mode.{parameters.blend_mode.value}"
                self._halo(node, "blend.opacity", mode_key)
                opacity = self._add(
                    f"{node.node_id}__opacity",
                    "blend.opacity",
                    {"value": parameters.opacity_u16 / 65535.0},
                    ((inputs[1], "input"),),
                )
                graph_outputs[node.node_id] = self._add(
                    f"{node.node_id}__blend",
                    mode_key,
                    {},
                    ((inputs[0], "input"), (opacity, "aux")),
                )
            elif isinstance(parameters, MaskParameters):
                keys = ["mask.apply"]
                mask_input = inputs[1]
                if parameters.inverted:
                    keys.append("mask.invert")
                    mask_input = self._add(
                        f"{node.node_id}__invert",
                        "mask.invert",
                        {},
                        ((mask_input, "input"),),
                    )
                self._halo(node, *keys)
                graph_outputs[node.node_id] = self._add(
                    f"{node.node_id}__mask",
                    "mask.apply",
                    {"value": 1.0},
                    ((inputs[0], "input"), (mask_input, "aux")),
                )
            elif isinstance(parameters, AdjustmentParameters):
                key = f"adjustment.{parameters.adjustment.adjustment_id.value}"
                self._halo(node, key)
                values = {
                    item.name: item.value for item in parameters.adjustment.parameters
                }
                graph_outputs[node.node_id] = self._add(
                    f"{node.node_id}__adjustment",
                    key,
                    values,
                    ((inputs[0], "input"),),
                )
            elif isinstance(parameters, OrderedGroupParameters):
                self._halo(node, "group.compose")
                output = inputs[0]
                for index, source in enumerate(inputs[1:], start=1):
                    output = self._add(
                        f"{node.node_id}__compose_{index}",
                        "group.compose",
                        {},
                        ((output, "input"), (source, "aux")),
                    )
                graph_outputs[node.node_id] = output
            elif isinstance(parameters, ColourConversionParameters):
                self._halo(node, "colour.cast", "colour.convert")
                if (
                    parameters.source_profile not in profiles
                    or parameters.destination_profile not in profiles
                ):
                    raise DecodeRefusal("colour conversion profile is not registered")
                cast = self._add(
                    f"{node.node_id}__cast",
                    "colour.cast",
                    {"profile": parameters.source_profile},
                    ((inputs[0], "input"),),
                )
                graph_outputs[node.node_id] = self._add(
                    f"{node.node_id}__convert",
                    "colour.convert",
                    {"profile": parameters.destination_profile},
                    ((cast, "input"),),
                )
            elif isinstance(parameters, DestinationCropScaleParameters):
                self._halo(node, "destination.scale", "destination.crop")
                scaled = self._add(
                    f"{node.node_id}__scale",
                    "destination.scale",
                    {
                        "x": parameters.destination.width / parameters.source.width,
                        "y": parameters.destination.height / parameters.source.height,
                    },
                    ((inputs[0], "input"),),
                )
                destination = parameters.destination
                graph_outputs[node.node_id] = self._add(
                    f"{node.node_id}__crop",
                    "destination.crop",
                    {
                        "x": destination.x,
                        "y": destination.y,
                        "width": destination.width,
                        "height": destination.height,
                    },
                    ((scaled, "input"),),
                )
            else:
                raise InvalidGraph("closed graph compiler received an unknown family")

        return CompiledGraphPlan(
            graph.digest,
            graph.revision,
            tuple(self._nodes),
            graph_outputs[graph.output_node],
        )

    @staticmethod
    def _resolve_source(
        node: GraphNodeSpec,
        revision: RevisionId,
        buffers: tuple[BufferRef, ...],
    ) -> BufferRef:
        parameters = node.parameters
        if isinstance(parameters, PixelSourceParameters):
            digest = parameters.object_digest
            extent = parameters.extent
        elif isinstance(parameters, TextSourceParameters):
            digest = parameters.render_identity
            extent = parameters.extent
        else:
            raise InvalidGraph("source node has the wrong parameter family")
        matches = tuple(
            item
            for item in buffers
            if item.content_digest == digest
            and item.extent == extent
            and item.spec == node.output_spec
            and item.revision == revision
        )
        if len(matches) != 1:
            raise DecodeRefusal("source node does not resolve to one imported buffer")
        return matches[0]


@dataclass(slots=True)
class _OwnedSession:
    root: pathlib.Path
    path: pathlib.Path
    token: str
    lock_fd: int


_SESSION_RE = re.compile(r"session-([0-9a-f]{32})\Z")
_MARKER_SCHEMA = "kilix.imageshop.gegl-swap-session/v1"


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _ensure_private_application_root(root: pathlib.Path) -> pathlib.Path:
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        application = root / "kilix-image-shop"
        try:
            application.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = application.lstat()
    except OSError as exc:
        raise ResourceExhaustion(
            "private engine directory cannot be established",
            diagnostic_ref="runtime.private-directory",
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ResourceExhaustion("private engine path is not a directory")
    if metadata.st_uid != os.getuid() or _mode(application) != 0o700:
        raise ResourceExhaustion("private engine directory ownership or mode is unsafe")
    try:
        if application.resolve().parent != root.resolve():
            raise ResourceExhaustion("private engine directory escaped its configured root")
    except OSError as exc:
        raise ResourceExhaustion("private engine directory cannot be resolved") from exc
    return application


def _read_marker(path: pathlib.Path) -> dict[str, object] | None:
    marker = path / "OWNER.json"
    try:
        metadata = marker.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return None
        if metadata.st_size <= 0 or metadata.st_size > 4096:
            return None
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"schema", "token", "uid"}:
        return None
    if value["schema"] != _MARKER_SCHEMA or value["uid"] != os.getuid():
        return None
    match = _SESSION_RE.fullmatch(path.name)
    if match is None or value["token"] != match.group(1):
        return None
    return value


def _remove_owned_session(root: pathlib.Path, path: pathlib.Path, token: str) -> None:
    if path.parent != root or _SESSION_RE.fullmatch(path.name) is None:
        raise ResourceExhaustion("swap cleanup target is outside the owned session root")
    marker = _read_marker(path)
    if marker is None or marker["token"] != token:
        raise ResourceExhaustion("swap cleanup target lacks the matching owner marker")
    try:
        if path.resolve().parent != root.resolve():
            raise ResourceExhaustion("swap cleanup target escaped the owned root")
        shutil.rmtree(path)
    except OSError as exc:
        raise ResourceExhaustion(
            "owned swap session cannot be removed",
            diagnostic_ref="runtime.swap-cleanup",
        ) from exc


def _cleanup_stale_sessions(root: pathlib.Path) -> None:
    try:
        candidates = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise ResourceExhaustion("swap session root cannot be enumerated") from exc
    for candidate in candidates:
        match = _SESSION_RE.fullmatch(candidate.name)
        if match is None or _read_marker(candidate) is None:
            continue
        lock_path = candidate / "LOCK"
        try:
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            _remove_owned_session(root, candidate, match.group(1))
        finally:
            os.close(lock_fd)


def _create_owned_session(configuration: RuntimeConfiguration) -> _OwnedSession:
    cache_application = _ensure_private_application_root(configuration.cache_root)
    _ensure_private_application_root(configuration.runtime_root)
    swap_root = cache_application / "gegl-swap"
    try:
        try:
            swap_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = swap_root.lstat()
    except OSError as exc:
        raise ResourceExhaustion("GEGL swap root cannot be established") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or _mode(swap_root) != 0o700
    ):
        raise ResourceExhaustion("GEGL swap root ownership or mode is unsafe")
    _cleanup_stale_sessions(swap_root)

    token = secrets.token_hex(16)
    session = swap_root / f"session-{token}"
    lock_fd = -1
    try:
        session.mkdir(mode=0o700)
        lock_fd = os.open(
            session / "LOCK",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker_bytes = (
            json.dumps(
                {"schema": _MARKER_SCHEMA, "token": token, "uid": os.getuid()},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        marker_fd = os.open(
            session / "OWNER.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            written = 0
            while written < len(marker_bytes):
                written += os.write(marker_fd, marker_bytes[written:])
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
    except OSError as exc:
        if lock_fd >= 0:
            os.close(lock_fd)
        for child_name in ("OWNER.json", "LOCK"):
            try:
                (session / child_name).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            session.rmdir()
        except OSError:
            pass
        raise ResourceExhaustion(
            "private GEGL swap session cannot be created",
            diagnostic_ref="runtime.swap-session",
        ) from exc
    return _OwnedSession(swap_root, session, token, lock_fd)


def _close_owned_session(session: _OwnedSession) -> None:
    try:
        _remove_owned_session(session.root, session.path, session.token)
    finally:
        os.close(session.lock_fd)


def _materialize_profile(
    session: _OwnedSession,
    payload: bytes,
    digest: ObjectId,
) -> pathlib.Path:
    profiles = session.path / "profiles"
    try:
        try:
            profiles.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = profiles.lstat()
    except OSError as exc:
        raise ResourceExhaustion("private profile directory cannot be established") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or _mode(profiles) != 0o700
    ):
        raise ResourceExhaustion("private profile directory ownership or mode is unsafe")
    path = profiles / f"{digest.value}.icc"
    descriptor = -1
    created = False
    try:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            created = True
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        except FileExistsError:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            file_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(file_metadata.st_mode)
                or file_metadata.st_uid != os.getuid()
                or stat.S_IMODE(file_metadata.st_mode) != 0o600
                or file_metadata.st_size != len(payload)
            ):
                raise ResourceExhaustion("private profile carrier metadata changed")
            chunks: list[bytes] = []
            remaining = file_metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 65_536))
                if not chunk:
                    raise ResourceExhaustion("private profile carrier ended early")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ResourceExhaustion("private profile carrier grew while reading")
            if ObjectId.from_bytes(b"".join(chunks)) != digest:
                raise ResourceExhaustion("private profile carrier identity changed")
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise ResourceExhaustion("private profile carrier cannot be materialized") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _verify_carrier(
    path: pathlib.Path,
    expected: ObjectId,
    *,
    maximum_bytes: int,
    label: str,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnavailableGroup(f"{label} is not a regular installed carrier")
        if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
            raise UnavailableGroup(f"{label} has an invalid byte size")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise UnavailableGroup(f"{label} ended before its recorded byte size")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise UnavailableGroup(f"{label} grew while being verified")
        digest = ObjectId.from_bytes(b"".join(chunks))
    except FileNotFoundError as exc:
        raise UnavailableGroup(f"{label} is not installed") from exc
    except OSError as exc:
        raise UnavailableGroup(f"{label} cannot be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if digest != expected:
        raise IncompatibleRuntime(f"{label} digest differs from accepted identity")


def _validate_environment() -> None:
    if os.environ.get("BABL_TOLERANCE") != "0.0":
        raise IncompatibleRuntime("BABL_TOLERANCE must already equal 0.0")
    if os.environ.get("PYTHONPATH"):
        raise IncompatibleRuntime("PYTHONPATH must be absent at engine startup")
    if sys.flags.isolated != 1:
        raise IncompatibleRuntime("the installed application must run under Python -I")
    current = pathlib.Path.cwd().resolve()
    for entry in sys.path:
        if entry == "":
            raise IncompatibleRuntime("current-directory import fallback is enabled")
        try:
            if pathlib.Path(entry).resolve() == current:
                raise IncompatibleRuntime("current-directory import fallback is enabled")
        except (OSError, TypeError):
            continue
    if "gi.repository.Gegl" in sys.modules or "gi.repository.Babl" in sys.modules:
        raise IncompatibleRuntime("GEGL or babl was imported before the runtime guard")


def _read_dpkg_versions() -> dict[str, str]:
    status_path = pathlib.Path("/var/lib/dpkg/status")
    try:
        data = status_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UnavailableGroup("Debian package identity database is unavailable") from exc
    wanted = {"libgegl-0.4-0t64", "libbabl-0.1-0", "python3-gi"}
    versions: dict[str, str] = {}
    for paragraph in data.split("\n\n"):
        fields: dict[str, str] = {}
        for line in paragraph.splitlines():
            if not line or line[0].isspace() or ": " not in line:
                continue
            key, value = line.split(": ", 1)
            if key in {"Package", "Status", "Version"}:
                fields[key] = value
        package = fields.get("Package")
        if package in wanted and fields.get("Status") == "install ok installed":
            version = fields.get("Version")
            if version:
                versions[package] = version
    if set(versions) != wanted:
        raise UnavailableGroup("the complete OD-7 package identity is unavailable")
    return versions


class _PyGiBackend:
    def __init__(self, gi_module: Any, babl_module: Any, gegl_module: Any) -> None:
        self._gi = gi_module
        self._babl = babl_module
        self._gegl = gegl_module
        self._initialized = False

    def initialize(self) -> None:
        self._gegl.init(None)
        self._initialized = True

    def configure(self, values: tuple[tuple[str, object], ...]) -> None:
        config = self._gegl.config()
        for name, value in values:
            config.set_property(name, value)

    def read_configuration(self, names: tuple[str, ...]) -> dict[str, object]:
        config = self._gegl.config()
        return {name: config.get_property(name) for name in names}

    def observe(self) -> NativeObservation:
        versions = _read_dpkg_versions()
        origin = pathlib.Path(self._gi.__file__).resolve()
        try:
            gi_digest = ObjectId.from_bytes(origin.read_bytes())
        except OSError as exc:
            raise IncompatibleRuntime("imported GI carrier cannot be hashed") from exc
        operations = tuple(sorted(set(str(item) for item in self._gegl.list_operations())))
        return NativeObservation(
            gegl_native_version=(
                f"{self._gegl.MAJOR_VERSION}."
                f"{self._gegl.MINOR_VERSION}."
                f"{self._gegl.MICRO_VERSION}"
            ),
            babl_native_version=(
                f"{self._babl.MAJOR_VERSION}."
                f"{self._babl.MINOR_VERSION}."
                f"{self._babl.MICRO_VERSION}"
            ),
            gegl_package_version=versions["libgegl-0.4-0t64"],
            babl_package_version=versions["libbabl-0.1-0"],
            python_gi_package_version=versions["python3-gi"],
            gi_origin=origin,
            gi_file_digest=gi_digest,
            operations=operations,
        )

    @staticmethod
    def _default_scalar(value: object) -> object:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        try:
            return int(value)  # GObject enum values
        except (TypeError, ValueError):
            raise IncompatibleRuntime(
                "native operation property has an unsupported default type"
            ) from None

    def verify_operation_registry(self, registry: OperationRegistry) -> None:
        graph = self._gegl.Node()
        native_nodes: list[object] = []
        try:
            for definition in registry.definitions:
                if not self._gegl.has_operation(definition.operation):
                    raise IncompatibleRuntime("operation registry names a missing operation")
                observed_properties = {
                    item.name: item
                    for item in self._gegl.Operation.list_properties(
                        definition.operation
                    )
                }
                if set(observed_properties) != {
                    item.native_name for item in definition.properties
                }:
                    raise IncompatibleRuntime(
                        "native operation property population differs from registry"
                    )
                for expected_property in definition.properties:
                    observed = observed_properties[expected_property.native_name]
                    if observed.value_type.name != expected_property.native_type:
                        raise IncompatibleRuntime(
                            "native operation property type differs from registry"
                        )
                    if self._default_scalar(
                        observed.get_default_value()
                    ) != expected_property.default_value:
                        raise IncompatibleRuntime(
                            "native operation property default differs from registry"
                        )
                node = graph.create_child(definition.operation)
                native_nodes.append(node)
                if tuple(node.list_input_pads()) != definition.input_pads:
                    raise IncompatibleRuntime(
                        "native operation input pads differ from registry"
                    )
                output_pads = tuple(node.list_output_pads())
                expected_outputs = (
                    ()
                    if definition.output_pad is None
                    else (definition.output_pad,)
                )
                if output_pads != expected_outputs:
                    raise IncompatibleRuntime(
                        "native operation output pads differ from registry"
                    )
        finally:
            native_nodes.clear()
            del graph
            gc.collect()

    def validate_profile(self, path: pathlib.Path, encoding: str) -> None:
        source_buffer = self._gegl.Buffer.new(encoding, 0, 0, 1, 1)
        destination_buffer = self._gegl.Buffer.new(encoding, 0, 0, 1, 1)
        graph = self._gegl.Node()
        source = graph.create_child("gegl:buffer-source")
        source.set_property("buffer", source_buffer)
        cast = graph.create_child("gegl:cast-space")
        cast.set_property("path", str(path))
        convert = graph.create_child("gegl:convert-space")
        convert.set_property("path", str(path))
        sink = graph.create_child("gegl:write-buffer")
        sink.set_property("buffer", destination_buffer)
        source.connect_to("output", cast, "input")
        cast.connect_to("output", convert, "input")
        convert.connect_to("output", sink, "input")
        sink.process()
        del sink, convert, cast, source, graph, destination_buffer, source_buffer
        gc.collect()

    def import_pixels(
        self,
        payload: bytes,
        extent: Rect,
        encoding: str,
    ) -> object:
        rectangle = self._gegl.Rectangle.new(
            extent.x,
            extent.y,
            extent.width,
            extent.height,
        )
        buffer = self._gegl.Buffer.new(
            encoding,
            extent.x,
            extent.y,
            extent.width,
            extent.height,
        )
        buffer.set(rectangle, encoding, payload)
        return buffer

    def duplicate_buffer(self, buffer: object) -> object:
        return buffer.dup()

    def write_buffer(
        self,
        buffer: object,
        rectangle: Rect,
        encoding: str,
        payload: bytes,
    ) -> None:
        native_rectangle = self._gegl.Rectangle.new(
            rectangle.x,
            rectangle.y,
            rectangle.width,
            rectangle.height,
        )
        buffer.set(native_rectangle, encoding, payload)

    def compile_plan(
        self,
        plan: CompiledGraphPlan,
        buffers: dict[str, object],
        profiles: dict[ObjectId, pathlib.Path],
    ) -> object:
        graph = self._gegl.Node()
        nodes: dict[str, object] = {}
        try:
            for planned in plan.nodes:
                node = graph.create_child(planned.native_operation)
                for property_value in planned.properties:
                    value: object = property_value.value
                    if isinstance(value, BufferBinding):
                        if value.token not in buffers:
                            raise InvalidGraph("compiled plan names an unknown buffer")
                        value = buffers[value.token]
                    elif isinstance(value, ProfileBinding):
                        if value.digest not in profiles:
                            raise InvalidGraph("compiled plan names an unknown profile")
                        value = str(profiles[value.digest])
                    node.set_property(property_value.native_name, value)
                for connection in planned.inputs:
                    source = nodes.get(connection.source_plan_id)
                    if source is None:
                        raise InvalidGraph("compiled plan has a forward native edge")
                    source.connect_to(
                        connection.source_pad,
                        node,
                        connection.destination_pad,
                    )
                nodes[planned.plan_id] = node
        except Exception:
            nodes.clear()
            del graph
            gc.collect()
            raise
        output = next(
            item for item in plan.nodes if item.plan_id == plan.output_plan_id
        )
        if output.output_pad is None:
            nodes.clear()
            del graph
            gc.collect()
            raise InvalidGraph("compiled graph output has no native output pad")
        return _NativeCompiledGraph(
            graph,
            nodes,
            plan.output_plan_id,
            output.output_pad,
        )

    @staticmethod
    def _set_proxy_properties(
        node: object,
        definition: OperationDefinition,
        semantic_values: dict[str, object],
    ) -> None:
        for property_value in _bind_properties(definition, semantic_values):
            if isinstance(property_value.value, (BufferBinding, ProfileBinding)):
                raise InvalidGraph("proxy operation contains an unresolved reference")
            node.set_property(property_value.native_name, property_value.value)

    @staticmethod
    def _set_buffer_property(
        node: object,
        definition: OperationDefinition,
        native_buffer: object,
    ) -> None:
        semantic = tuple(definition.semantic_properties)
        if (
            len(semantic) != 1
            or semantic[0].semantic_name != "buffer"
            or semantic[0].value_kind is not RegistryValueKind.BUFFER
        ):
            raise IncompatibleRuntime("native operation is not buffer-bound")
        for property_specification in definition.properties:
            if property_specification.source is PropertySource.DEFAULT:
                continue
            if property_specification.source is PropertySource.FIXED:
                value = _binding_value(
                    property_specification,
                    property_specification.fixed_value,
                )
            else:
                value = native_buffer
            node.set_property(property_specification.native_name, value)

    def build_proxy(
        self,
        graph: object,
        *,
        level: int,
        expected_extent: Rect,
        encoding: str,
        scale_definition: OperationDefinition,
        sink_definition: OperationDefinition,
    ) -> object:
        if not isinstance(graph, _NativeCompiledGraph) or graph.root is None:
            raise InvalidGraph("proxy build requires a live compiled graph")
        if level not in {1, 2, 3}:
            raise InvalidGraph("proxy build received an invalid level")
        root = graph.root
        output = graph.nodes.get(graph.output_plan_id)
        if output is None:
            raise InvalidGraph("compiled graph output is unavailable")
        if (
            len(scale_definition.input_pads) != 1
            or scale_definition.output_pad is None
            or len(sink_definition.input_pads) != 1
            or sink_definition.output_pad is not None
        ):
            raise IncompatibleRuntime("proxy operation pads differ from the closed map")
        scale_key = f"__proxy_{level}_scale"
        sink_key = f"__proxy_{level}_sink"
        scale = graph.nodes.get(scale_key)
        if scale is None:
            scale = root.create_child(scale_definition.operation)
            ratio = 1.0 / (1 << level)
            self._set_proxy_properties(
                scale,
                scale_definition,
                {"x": ratio, "y": ratio},
            )
            output.connect_to(
                graph.output_pad,
                scale,
                scale_definition.input_pads[0],
            )
        bounding_box = scale.get_bounding_box()
        observed_extent = Rect(
            int(bounding_box.x),
            int(bounding_box.y),
            int(bounding_box.width),
            int(bounding_box.height),
        )
        if observed_extent != expected_extent:
            raise IncompatibleRuntime("native proxy extent differs from checked geometry")
        destination = self._gegl.Buffer.new(
            encoding,
            expected_extent.x,
            expected_extent.y,
            expected_extent.width,
            expected_extent.height,
        )
        sink = graph.nodes.get(sink_key)
        new_sink = sink is None
        if sink is None:
            sink = root.create_child(sink_definition.operation)
        self._set_buffer_property(sink, sink_definition, destination)
        if new_sink:
            scale.connect_to(
                scale_definition.output_pad,
                sink,
                sink_definition.input_pads[0],
            )
        sink.process()
        graph.nodes[scale_key] = scale
        graph.nodes[sink_key] = sink
        return destination

    def read_buffer(
        self,
        buffer: object,
        rectangle: Rect,
        encoding: str,
    ) -> bytes:
        native_rectangle = self._gegl.Rectangle.new(
            rectangle.x,
            rectangle.y,
            rectangle.width,
            rectangle.height,
        )
        payload = buffer.get(
            native_rectangle,
            1.0,
            encoding,
            self._gegl.AbyssPolicy.NONE,
        )
        return bytes(payload)

    def render_tile(
        self,
        source: object,
        *,
        source_rectangle: Rect,
        destination_rectangle: Rect,
        encoding: str,
        source_definition: OperationDefinition,
        crop_definition: OperationDefinition,
        scale_definition: OperationDefinition,
    ) -> bytes:
        if (
            len(crop_definition.input_pads) != 1
            or crop_definition.output_pad is None
            or len(scale_definition.input_pads) != 1
            or scale_definition.output_pad is None
        ):
            raise IncompatibleRuntime("tile adapter pads differ from the closed map")
        owned_root = not isinstance(source, _NativeCompiledGraph)
        if owned_root:
            root = self._gegl.Node()
            source_node = root.create_child(source_definition.operation)
            self._set_buffer_property(source_node, source_definition, source)
            output = source_node
            output_pad = source_definition.output_pad
            if output_pad is None:
                raise IncompatibleRuntime("tile buffer source has no output pad")
        else:
            if source.root is None:
                raise InvalidGraph("tile render requires a live compiled graph")
            root = source.root
            output = source.nodes.get(source.output_plan_id)
            output_pad = source.output_pad
            if output is None:
                raise InvalidGraph("compiled tile source output is unavailable")
            source_node = None
        transient_nodes: list[object] = []
        destination_buffer: object | None = None
        try:
            crop = root.create_child(crop_definition.operation)
            transient_nodes.append(crop)
            self._set_proxy_properties(
                crop,
                crop_definition,
                {
                    "x": source_rectangle.x,
                    "y": source_rectangle.y,
                    "width": source_rectangle.width,
                    "height": source_rectangle.height,
                },
            )
            output.connect_to(output_pad, crop, crop_definition.input_pads[0])
            output = crop
            output_pad = crop_definition.output_pad
            if (
                source_rectangle.width != destination_rectangle.width
                or source_rectangle.height != destination_rectangle.height
            ):
                scale = root.create_child(scale_definition.operation)
                transient_nodes.append(scale)
                self._set_proxy_properties(
                    scale,
                    scale_definition,
                    {
                        "x": destination_rectangle.width / source_rectangle.width,
                        "y": destination_rectangle.height / source_rectangle.height,
                    },
                )
                output.connect_to(
                    output_pad,
                    scale,
                    scale_definition.input_pads[0],
                )
                output = scale
                output_pad = scale_definition.output_pad
            bounding_box = output.get_bounding_box()
            native_rectangle = self._gegl.Rectangle.new(
                int(bounding_box.x),
                int(bounding_box.y),
                int(bounding_box.width),
                int(bounding_box.height),
            )
            if (
                native_rectangle.width != destination_rectangle.width
                or native_rectangle.height != destination_rectangle.height
            ):
                raise IncompatibleRuntime(
                    "native tile adapter extent differs from checked geometry"
                )
            destination_buffer = self._gegl.Buffer.new(
                encoding,
                native_rectangle.x,
                native_rectangle.y,
                native_rectangle.width,
                native_rectangle.height,
            )
            output.blit_buffer(
                destination_buffer,
                native_rectangle,
                0,
                self._gegl.AbyssPolicy.NONE,
            )
            payload = destination_buffer.get(
                native_rectangle,
                1.0,
                encoding,
                self._gegl.AbyssPolicy.NONE,
            )
            return bytes(payload)
        finally:
            if not owned_root:
                for node in reversed(transient_nodes):
                    try:
                        node.disconnect("input")
                    except Exception:
                        pass
                    try:
                        root.remove_child(node)
                    except Exception:
                        pass
            transient_nodes.clear()
            destination_buffer = None
            if owned_root:
                source_node = None
                del root
            gc.collect()

    def release_buffer(self, buffer: object) -> None:
        del buffer
        gc.collect()

    def release_graph(self, graph: object) -> None:
        if not isinstance(graph, _NativeCompiledGraph):
            raise InternalEngineFailure("runtime received an unknown compiled graph")
        graph.nodes.clear()
        graph.root = None
        gc.collect()

    def smoke_test(self) -> None:
        source_buffer = self._gegl.Buffer.new("RGBA u16", 0, 0, 1, 1)
        destination_buffer = self._gegl.Buffer.new("RGBA u16", 0, 0, 1, 1)
        graph = self._gegl.Node()
        source = graph.create_child("gegl:buffer-source")
        source.set_property("buffer", source_buffer)
        sink = graph.create_child("gegl:write-buffer")
        sink.set_property("buffer", destination_buffer)
        source.connect_to("output", sink, "input")
        sink.process()
        del sink, source, graph, destination_buffer, source_buffer
        gc.collect()

    def shutdown(self) -> None:
        if self._initialized:
            self._gegl.exit()
            self._initialized = False


@dataclass(slots=True)
class _NativeCompiledGraph:
    root: object | None
    nodes: dict[str, object]
    output_plan_id: str
    output_pad: str


def _load_pygi() -> NativeRuntimeBackend:
    try:
        import gi
    except (ImportError, ModuleNotFoundError) as exc:
        raise UnavailableGroup(
            "Debian python3-gi is unavailable",
            diagnostic_ref="runtime.gi-import",
        ) from exc
    try:
        origin = pathlib.Path(gi.__file__).resolve()
    except (AttributeError, OSError, TypeError) as exc:
        raise IncompatibleRuntime("GI import has no canonical archive origin") from exc
    if origin != GI_ORIGIN:
        raise IncompatibleRuntime("GI import did not resolve to the Debian archive path")
    try:
        gi.require_version("Gegl", "0.4")
        gi.require_version("Babl", "0.1")
        from gi.repository import Babl, Gegl
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        raise UnavailableGroup(
            "required GEGL 0.4 or babl 0.1 namespace is unavailable",
            diagnostic_ref="runtime.gi-namespace",
        ) from exc
    return _PyGiBackend(gi, Babl, Gegl)


class ImageRuntime:
    """Coordinates the ten-step startup and one-way orderly shutdown."""

    def __init__(
        self,
        configuration: RuntimeConfiguration,
        *,
        native_loader: Callable[[], NativeRuntimeBackend] = _load_pygi,
        process_guard: RuntimeProcessGuard = _GLOBAL_PROCESS_GUARD,
    ) -> None:
        if not isinstance(configuration, RuntimeConfiguration):
            raise InvalidGraph("image runtime requires a validated configuration")
        if not callable(native_loader):
            raise InvalidGraph("native loader must be callable")
        if not isinstance(process_guard, RuntimeProcessGuard):
            raise InvalidGraph("runtime process guard has the wrong type")
        self._configuration = configuration
        self._native_loader = native_loader
        self._guard = process_guard
        self._native: NativeRuntimeBackend | None = None
        self._session: _OwnedSession | None = None
        self._handle: RuntimeHandle | None = None
        self._owner_thread: int | None = None

    @property
    def handle(self) -> RuntimeHandle | None:
        return self._handle

    def _check_owner(self) -> None:
        current = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = current
        elif self._owner_thread != current:
            raise InternalEngineFailure(
                "runtime lifecycle is restricted to its owner executor",
                diagnostic_ref="runtime.owner-thread",
            )

    def _configuration_values(self, swap: pathlib.Path) -> tuple[tuple[str, object], ...]:
        expected = self._configuration.expected
        return (
            ("tile-cache-size", expected.tile_cache_bytes),
            ("use-opencl", expected.use_opencl),
            ("swap", str(swap)),
            ("swap-compression", expected.swap_compression),
            ("mipmap-rendering", self._configuration.mipmap_rendering),
            ("threads", expected.threads),
        )

    @staticmethod
    def _verify_readback(
        wanted: tuple[tuple[str, object], ...],
        observed: dict[str, object],
    ) -> None:
        if set(observed) != {name for name, _ in wanted}:
            raise IncompatibleRuntime("GEGL configuration readback is incomplete")
        for name, value in wanted:
            actual = observed[name]
            if type(actual) is not type(value) or actual != value:
                raise IncompatibleRuntime(f"GEGL configuration readback differs: {name}")

    def start(self) -> RuntimeHandle:
        self._check_owner()
        self._guard.begin()
        completed: list[StartupStep] = []
        native: NativeRuntimeBackend | None = None
        session: _OwnedSession | None = None
        try:
            configuration = self._configuration
            _verify_carrier(
                configuration.package_group_record,
                configuration.expected.package_group_digest,
                maximum_bytes=configuration.MAX_CARRIER_BYTES,
                label="package-group record",
            )
            _verify_carrier(
                configuration.plugin_tree_manifest,
                configuration.expected.plugin_tree_digest,
                maximum_bytes=configuration.MAX_CARRIER_BYTES,
                label="plugin-tree manifest",
            )
            completed.append(StartupStep.VALIDATE_CONFIGURATION)

            _validate_environment()
            completed.append(StartupStep.VALIDATE_ENVIRONMENT)

            session = _create_owned_session(configuration)
            completed.append(StartupStep.CREATE_PRIVATE_PATHS)

            native = self._native_loader()
            completed.append(StartupStep.LOAD_GI_NAMESPACES)

            native.initialize()
            completed.append(StartupStep.INITIALIZE_GEGL)

            wanted = self._configuration_values(session.path)
            native.configure(wanted)
            completed.append(StartupStep.APPLY_CONFIGURATION)

            observed_configuration = native.read_configuration(
                tuple(name for name, _ in wanted)
            )
            self._verify_readback(wanted, observed_configuration)
            completed.append(StartupStep.READ_BACK_CONFIGURATION)

            validate_native_observation(configuration, native.observe())
            native.verify_operation_registry(configuration.operation_registry)
            completed.append(StartupStep.VERIFY_COMPATIBILITY)

            native.smoke_test()
            completed.append(StartupStep.RUN_SMOKE_GRAPH)

            compatibility_digest = configuration.expected.digest
            capabilities = EngineCapabilities(
                engine_id="kilix.gegl-babl-od7/v1",
                compatibility_digest=compatibility_digest,
                supported_formats=(PixelFormat.RGBA_U16, PixelFormat.Y_U8),
                supported_nodes=tuple(GraphNodeKind),
                proxy_levels=(1, 2, 3),
                max_tile_width=1920,
                max_tile_height=1080,
            )
            completed.append(StartupStep.PUBLISH_HANDLE)
            handle = RuntimeHandle(
                configuration.expected,
                capabilities,
                session.path,
                tuple(completed),
            )
            self._guard.publish()
            self._native = native
            self._session = session
            self._handle = handle
            return handle
        except Exception as exc:
            if native is not None:
                try:
                    native.shutdown()
                except Exception:
                    pass
            if session is not None:
                try:
                    _close_owned_session(session)
                except Exception:
                    pass
            self._guard.fail()
            if isinstance(
                exc,
                (
                    IncompatibleRuntime,
                    InternalEngineFailure,
                    InvalidGraph,
                    ResourceExhaustion,
                    UnavailableGroup,
                ),
            ):
                raise
            raise InternalEngineFailure(
                "native runtime initialization failed",
                diagnostic_ref="runtime.initialization",
            ) from exc

    def close(self) -> None:
        self._check_owner()
        if self._native is None or self._session is None or self._handle is None:
            raise IncompatibleRuntime("runtime is not published")
        native = self._native
        session = self._session
        failure: Exception | None = None
        try:
            native.shutdown()
        except Exception as exc:
            failure = exc
        try:
            _close_owned_session(session)
        except Exception as exc:
            if failure is None:
                failure = exc
        self._native = None
        self._session = None
        self._handle = None
        self._guard.close()
        if failure is not None:
            if isinstance(failure, ResourceExhaustion):
                raise failure
            raise InternalEngineFailure(
                "orderly native runtime shutdown failed",
                diagnostic_ref="runtime.shutdown",
            ) from failure

    def _engine_state(self) -> tuple[NativeRuntimeBackend, _OwnedSession]:
        self._check_owner()
        if self._native is None or self._session is None or self._handle is None:
            raise IncompatibleRuntime("runtime is not published")
        return self._native, self._session


def _proxy_extent(source: Rect, level: int) -> Rect:
    if level not in {1, 2, 3}:
        raise InvalidGraph("proxy level must be in [1, 3]")
    denominator = 1 << level
    left = source.x // denominator
    top = source.y // denominator
    right = -(-(source.x + source.width) // denominator)
    bottom = -(-(source.y + source.height) // denominator)
    return Rect(left, top, right - left, bottom - top)


def _rectangles_intersect(left: Rect, right: Rect) -> bool:
    return not (
        left.x + left.width <= right.x
        or right.x + right.width <= left.x
        or left.y + left.height <= right.y
        or right.y + right.height <= left.y
    )


def _validate_proxy_requests(
    graph: GraphSpec,
    requests: tuple[TileRequest, ...],
) -> tuple[int, Rect]:
    if not isinstance(requests, tuple) or not requests or any(
        not isinstance(item, TileRequest) for item in requests
    ):
        raise InvalidGraph("proxy requests must be a non-empty immutable typed tuple")
    level = requests[0].level
    expected_extent = _proxy_extent(
        graph.nodes[-1].parameters.destination,
        level,
    )
    expected_identity = (
        graph.digest,
        level,
        graph.output_spec,
        graph.revision,
    )
    rectangles: list[Rect] = []
    denominator = 1 << level
    source_extent = graph.nodes[-1].parameters.destination
    for request in requests:
        if (
            request.graph_digest,
            request.level,
            request.spec,
            request.revision,
        ) != expected_identity:
            raise InvalidGraph("proxy requests do not share the compiled graph identity")
        if not request.destination.is_within(expected_extent):
            raise InvalidGraph("proxy tile leaves the checked level extent")
        source_left = max(source_extent.x, request.destination.x * denominator)
        source_top = max(source_extent.y, request.destination.y * denominator)
        source_right = min(
            source_extent.x + source_extent.width,
            (request.destination.x + request.destination.width) * denominator,
        )
        source_bottom = min(
            source_extent.y + source_extent.height,
            (request.destination.y + request.destination.height) * denominator,
        )
        if request.source != Rect(
            source_left,
            source_top,
            source_right - source_left,
            source_bottom - source_top,
        ):
            raise InvalidGraph("proxy tile source mapping differs from checked scaling")
        if any(_rectangles_intersect(request.destination, item) for item in rectangles):
            raise InvalidGraph("proxy tile requests overlap")
        rectangles.append(request.destination)
    keys = tuple(
        (item.y, item.x, item.height, item.width) for item in rectangles
    )
    if keys != tuple(sorted(set(keys))):
        raise InvalidGraph("proxy tile requests must be sorted and unique")
    if sum(item.width * item.height for item in rectangles) != (
        expected_extent.width * expected_extent.height
    ):
        raise InvalidGraph("proxy requests do not cover one complete level")
    return level, expected_extent


def _process_memory_diagnostics() -> ProcessMemoryDiagnostics:
    try:
        payload = pathlib.Path("/proc/self/status").read_text(encoding="ascii")
        values: dict[str, int] = {}
        for line in payload.splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                name, amount, unit = line.split()
                if unit != "kB":
                    raise ValueError("unexpected process-memory unit")
                values[name.rstrip(":")] = int(amount) * 1024
        resident = values["VmRSS"]
        peak = values["VmHWM"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError) as exc:
        raise InternalEngineFailure(
            "process memory diagnostics are unavailable",
            diagnostic_ref="engine.diagnostics-memory",
        ) from exc
    return ProcessMemoryDiagnostics(resident, max(resident, peak))


def _swap_diagnostics(session: _OwnedSession) -> SwapDiagnostics:
    bytes_used = 0
    file_count = 0
    try:
        for root, directories, files in os.walk(session.path, followlinks=False):
            root_path = pathlib.Path(root)
            directories[:] = [
                name
                for name in directories
                if name != "profiles"
                and not stat.S_ISLNK((root_path / name).lstat().st_mode)
            ]
            for name in files:
                if name in {"LOCK", "OWNER.json"}:
                    continue
                metadata = (root_path / name).lstat()
                if stat.S_ISREG(metadata.st_mode):
                    bytes_used += metadata.st_size
                    file_count += 1
    except OSError as exc:
        raise InternalEngineFailure(
            "swap diagnostics are unavailable",
            diagnostic_ref="engine.diagnostics-swap",
        ) from exc
    return SwapDiagnostics(bytes_used, file_count, None)


class Od7ImageEngine:
    """H0 GEGL/babl engine whose native handles remain inside this module."""

    def __init__(self, runtime: ImageRuntime) -> None:
        if not isinstance(runtime, ImageRuntime):
            raise InvalidGraph("OD-7 engine requires a guarded image runtime")
        expected = runtime._configuration.expected
        self._runtime = runtime
        self._policy = TierFormatPolicy(
            RenderTier.H0,
            PixelFormat(expected.working_format),
            expected.alpha_association,
        )
        self._capabilities: EngineCapabilities | None = None
        self._profiles: dict[ObjectId, pathlib.Path] = {}
        self._buffers: dict[str, tuple[BufferRef, object]] = {}
        self._mask_indexes: dict[str, tuple[MaskTileDigest, ...]] = {}
        self._graphs: dict[ObjectId, object] = {}
        self._plans: dict[ObjectId, CompiledGraphPlan] = {}
        self._graph_specs: dict[ObjectId, GraphSpec] = {}
        self._proxies: dict[tuple[ObjectId, int], object] = {}
        self._proxy_results: dict[
            tuple[ObjectId, int], tuple[TileResult, ...]
        ] = {}
        self._running_tiles = 0
        self._tile_timings: list[int] = []

    def _state(self) -> tuple[NativeRuntimeBackend, _OwnedSession]:
        if self._capabilities is None:
            raise IncompatibleRuntime("OD-7 engine is not started")
        return self._runtime._engine_state()

    def start(self) -> EngineCapabilities:
        if self._capabilities is not None:
            raise IncompatibleRuntime("OD-7 engine is already started")
        handle = self._runtime.start()
        self._capabilities = handle.capabilities
        return handle.capabilities

    def register_profile(
        self,
        payload: bytes,
        digest: ObjectId,
        *,
        cancel: CancelToken,
    ) -> ObjectId:
        backend, session = self._state()
        cancel.raise_if_cancelled()
        if not isinstance(payload, bytes) or not 0 < len(payload) <= 4_194_304:
            raise DecodeRefusal("ICC profile must be bounded immutable bytes")
        if not isinstance(digest, ObjectId) or ObjectId.from_bytes(payload) != digest:
            raise DecodeRefusal("ICC profile bytes do not match their content identity")
        if digest in self._profiles:
            return digest
        path = _materialize_profile(session, payload, digest)
        try:
            probe_spec = self._policy.colour_spec(digest)
            backend.validate_profile(path, self._policy.native_encoding(probe_spec))
            cancel.raise_if_cancelled()
        except Exception as exc:
            try:
                path.unlink()
            except OSError:
                pass
            if isinstance(exc, EngineFailure):
                raise
            raise DecodeRefusal(
                "ICC profile was refused by the image engine",
                diagnostic_ref="engine.profile-validation",
            ) from exc
        self._profiles[digest] = path
        return digest

    def import_pixels(
        self,
        payload: bytes,
        *,
        extent: Rect,
        spec: PixelSpec,
        revision: RevisionId,
        cancel: CancelToken,
    ) -> BufferRef:
        backend, _ = self._state()
        cancel.raise_if_cancelled()
        if not isinstance(extent, Rect) or not isinstance(revision, RevisionId):
            raise DecodeRefusal("decoded pixels require typed extent and revision")
        self._policy.validate(spec)
        if spec.profile_digest is not None and spec.profile_digest not in self._profiles:
            raise DecodeRefusal("decoded pixel profile is not registered")
        self._policy.validate_payload(payload, extent.width, extent.height, spec)
        mask_index: tuple[MaskTileDigest, ...] | None = None
        if spec == PixelSpec.foreground_mask():
            mask_index = mask_digest_index(payload, extent)
            digest = mask_manifest_digest(extent, mask_index)
        else:
            digest = ObjectId.from_bytes(payload)
        descriptor = json.dumps(
            {
                "content": digest.value,
                "extent": extent.to_data(),
                "spec": spec.to_data(),
                "revision": revision.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        token = f"od7:{hashlib.sha256(descriptor).hexdigest()}"
        existing = self._buffers.get(token)
        if existing is not None:
            return existing[0]
        try:
            native = backend.import_pixels(
                payload,
                extent,
                self._policy.native_encoding(spec),
            )
            cancel.raise_if_cancelled()
        except Exception as exc:
            if "native" in locals():
                try:
                    backend.release_buffer(native)
                except Exception:
                    pass
            if isinstance(exc, EngineFailure):
                raise
            raise DecodeRefusal(
                "decoded pixels were refused by the image engine",
                diagnostic_ref="engine.pixel-import",
            ) from exc
        reference = BufferRef(token, extent, spec, revision, digest)
        self._buffers[token] = (reference, native)
        if mask_index is not None:
            self._mask_indexes[token] = mask_index
        return reference

    def edit_mask(
        self,
        buffer: BufferRef,
        updates: tuple[MaskTileUpdate, ...],
        *,
        new_revision: RevisionId,
        cancel: CancelToken,
    ) -> BufferRef:
        backend, _ = self._state()
        cancel.raise_if_cancelled()
        if not isinstance(buffer, BufferRef) or buffer.token not in self._buffers:
            raise InvalidGraph("mask edit requires one live opaque buffer reference")
        if self._buffers[buffer.token][0] != buffer:
            raise InvalidGraph("mask edit buffer identity differs from the live reference")
        if buffer.spec != PixelSpec.foreground_mask():
            raise InvalidGraph("mask edit requires a Y u8 foreground-alpha buffer")
        if not isinstance(new_revision, RevisionId) or new_revision == buffer.revision:
            raise InvalidGraph("mask edit must advance the typed buffer revision")
        if not isinstance(updates, tuple) or not updates or any(
            not isinstance(item, MaskTileUpdate) for item in updates
        ):
            raise InvalidGraph("mask edit requires immutable typed tile updates")
        keys = tuple((item.rectangle.y, item.rectangle.x) for item in updates)
        if keys != tuple(sorted(set(keys))):
            raise InvalidGraph("mask updates must be sorted and unique")
        current_index = self._mask_indexes[buffer.token]
        current = {item.rectangle: item.digest for item in current_index}
        for update in updates:
            if update.rectangle not in current:
                raise InvalidGraph("mask update is not one exact sparse tile")
            if current[update.rectangle] != update.before_digest:
                raise CancelledOrStaleWork("mask tile before-digest is stale")
            if update.after_digest == update.before_digest:
                raise InvalidGraph("mask update cannot be a no-op")
        revised = dict(current)
        for update in updates:
            revised[update.rectangle] = update.after_digest
        revised_index = tuple(
            MaskTileDigest(item.rectangle, revised[item.rectangle])
            for item in current_index
        )
        content_digest = mask_manifest_digest(buffer.extent, revised_index)
        descriptor = json.dumps(
            {
                "base": buffer.content_digest.value,
                "content": content_digest.value,
                "revision": new_revision.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        token = f"od7:{hashlib.sha256(descriptor).hexdigest()}"
        existing = self._buffers.get(token)
        if existing is not None:
            return existing[0]
        native: object | None = None
        try:
            native = backend.duplicate_buffer(self._buffers[buffer.token][1])
            cancel.raise_if_cancelled()
            for update in updates:
                cancel.raise_if_cancelled()
                backend.write_buffer(
                    native,
                    update.rectangle,
                    self._policy.native_encoding(buffer.spec),
                    update.payload,
                )
                cancel.raise_if_cancelled()
        except Exception as exc:
            if native is not None:
                try:
                    backend.release_buffer(native)
                except Exception:
                    pass
            if isinstance(exc, EngineFailure):
                raise
            raise InternalEngineFailure(
                "sparse native mask edit failed",
                diagnostic_ref="engine.mask-edit",
            ) from exc
        result = BufferRef(
            token,
            buffer.extent,
            buffer.spec,
            new_revision,
            content_digest,
        )
        self._buffers[token] = (result, native)
        self._mask_indexes[token] = revised_index
        return result

    def compile_graph(self, graph: GraphSpec, *, cancel: CancelToken) -> ObjectId:
        backend, _ = self._state()
        cancel.raise_if_cancelled()
        if not isinstance(graph, GraphSpec):
            raise InvalidGraph("OD-7 compiler requires a closed graph spec")
        assert self._capabilities is not None
        if graph.compatibility_digest != self._capabilities.compatibility_digest:
            raise IncompatibleRuntime("graph compatibility identity differs from runtime")
        if graph.digest in self._graphs:
            return graph.digest
        for node in graph.nodes:
            self._policy.validate(node.output_spec)
            profile = node.output_spec.profile_digest
            if profile is not None and profile not in self._profiles:
                raise DecodeRefusal("graph output profile is not registered")
        compiler = _GraphPlanCompiler(self._runtime._configuration.operation_registry)
        plan = compiler.compile(
            graph,
            tuple(item[0] for item in self._buffers.values()),
            tuple(sorted(self._profiles)),
        )
        try:
            native = backend.compile_plan(
                plan,
                {token: item[1] for token, item in self._buffers.items()},
                dict(self._profiles),
            )
            cancel.raise_if_cancelled()
        except Exception as exc:
            if "native" in locals():
                try:
                    backend.release_graph(native)
                except Exception:
                    pass
            if isinstance(exc, EngineFailure):
                raise
            raise InvalidGraph(
                "closed graph compilation failed",
                diagnostic_ref="engine.graph-compile",
            ) from exc
        self._graphs[graph.digest] = native
        self._plans[graph.digest] = plan
        self._graph_specs[graph.digest] = graph
        return graph.digest

    def compiled_plan(self, graph_digest: ObjectId) -> CompiledGraphPlan:
        self._state()
        try:
            return self._plans[graph_digest]
        except KeyError as exc:
            raise InvalidGraph("graph has not been compiled") from exc

    def render_tile(self, request: TileRequest, *, cancel: CancelToken) -> TileResult:
        backend, _ = self._state()
        cancel.raise_if_cancelled()
        if not isinstance(request, TileRequest):
            raise InvalidGraph("tile render requires a typed request")
        graph = self._graph_specs.get(request.graph_digest)
        if graph is None:
            raise InvalidGraph("tile request names an uncompiled graph")
        if request.revision != graph.revision:
            raise CancelledOrStaleWork("tile request revision is stale")
        if request.spec != graph.output_spec:
            raise InvalidGraph("tile request pixel spec differs from graph output")
        self._policy.validate(request.spec)
        graph_extent = graph.nodes[-1].parameters.destination
        if request.level == 0:
            source_extent = graph_extent
            native_source = self._graphs[graph.digest]
        else:
            source_extent = _proxy_extent(graph_extent, request.level)
            native_source = self._proxies.get((graph.digest, request.level))
            if native_source is None:
                raise InvalidGraph("tile request names an unavailable proxy level")
        if not request.source.is_within(source_extent):
            raise InvalidGraph("tile source rectangle leaves its selected render level")
        registry = self._runtime._configuration.operation_registry
        encoding = self._policy.native_encoding(request.spec)
        started_ns = time.monotonic_ns()
        self._running_tiles += 1
        try:
            try:
                payload = backend.render_tile(
                    native_source,
                    source_rectangle=request.source,
                    destination_rectangle=request.destination,
                    encoding=encoding,
                    source_definition=registry.definition("source.pixel"),
                    crop_definition=registry.definition("destination.crop"),
                    scale_definition=registry.definition("destination.scale"),
                )
                elapsed_ns = time.monotonic_ns() - started_ns
                self._tile_timings.append(elapsed_ns)
                del self._tile_timings[:-32]
            finally:
                self._running_tiles -= 1
            cancel.raise_if_cancelled()
            return TileResult(
                request.source,
                request.destination,
                request.level,
                request.spec,
                request.revision,
                ObjectId.from_bytes(payload),
                elapsed_ns,
                owned_bytes=payload,
            )
        except Exception as exc:
            if isinstance(exc, EngineFailure):
                raise
            raise InternalEngineFailure(
                "bounded native tile render failed",
                diagnostic_ref="engine.tile-render",
            ) from exc

    def build_proxy(
        self,
        requests: tuple[TileRequest, ...],
        *,
        cancel: CancelToken,
    ) -> tuple[TileResult, ...]:
        backend, _ = self._state()
        cancel.raise_if_cancelled()
        if not isinstance(requests, tuple) or not requests:
            raise InvalidGraph("proxy work requires a non-empty immutable request tuple")
        if any(not isinstance(item, TileRequest) for item in requests):
            raise InvalidGraph("proxy requests must be typed tile requests")
        graph = self._graph_specs.get(requests[0].graph_digest)
        if graph is None:
            raise InvalidGraph("proxy request names an uncompiled graph")
        level, extent = _validate_proxy_requests(graph, requests)
        cache_key = (graph.digest, level)
        existing = self._proxy_results.get(cache_key)
        if existing is not None:
            return existing
        native_graph = self._graphs[graph.digest]
        registry = self._runtime._configuration.operation_registry
        scale_definition = registry.definition("destination.scale")
        sink_definition = registry.definition("sink.write-buffer")
        encoding = self._policy.native_encoding(graph.output_spec)
        native_proxy: object | None = None
        try:
            cancel.raise_if_cancelled()
            native_proxy = backend.build_proxy(
                native_graph,
                level=level,
                expected_extent=extent,
                encoding=encoding,
                scale_definition=scale_definition,
                sink_definition=sink_definition,
            )
            cancel.raise_if_cancelled()
            completed: list[TileResult] = []
            for request in requests:
                cancel.raise_if_cancelled()
                started_ns = time.monotonic_ns()
                payload = backend.read_buffer(
                    native_proxy,
                    request.destination,
                    encoding,
                )
                elapsed_ns = time.monotonic_ns() - started_ns
                cancel.raise_if_cancelled()
                completed.append(
                    TileResult(
                        request.source,
                        request.destination,
                        request.level,
                        request.spec,
                        request.revision,
                        ObjectId.from_bytes(payload),
                        elapsed_ns,
                        owned_bytes=payload,
                    )
                )
            cancel.raise_if_cancelled()
        except Exception as exc:
            if native_proxy is not None:
                try:
                    backend.release_buffer(native_proxy)
                except Exception:
                    pass
            if isinstance(exc, EngineFailure):
                raise
            raise InternalEngineFailure(
                "native proxy construction failed",
                diagnostic_ref="engine.proxy-build",
            ) from exc
        results = tuple(completed)
        self._proxies[cache_key] = native_proxy
        self._proxy_results[cache_key] = results
        return results

    def invalidate_proxies(self, graph_digests: tuple[ObjectId, ...]) -> int:
        backend, _ = self._state()
        if not isinstance(graph_digests, tuple) or any(
            not isinstance(item, ObjectId) for item in graph_digests
        ):
            raise InvalidGraph("proxy invalidation requires typed graph digests")
        identities = tuple(item.value for item in graph_digests)
        if identities != tuple(sorted(set(identities))):
            raise InvalidGraph("proxy invalidation graph digests must be sorted and unique")
        wanted = set(graph_digests)
        selected = tuple(key for key in self._proxies if key[0] in wanted)
        failure: Exception | None = None
        for key in selected:
            native = self._proxies.pop(key)
            self._proxy_results.pop(key, None)
            try:
                backend.release_buffer(native)
            except Exception as exc:
                if failure is None:
                    failure = exc
            finally:
                del native
        gc.collect()
        if failure is not None:
            raise InternalEngineFailure(
                "native proxy invalidation failed",
                diagnostic_ref="engine.proxy-invalidation",
            ) from failure
        return len(selected)

    def diagnostics(self) -> EngineDiagnostics:
        _, session = self._state()
        entries = tuple(
            BufferInventoryEntry(reference.extent, reference.spec, reference.revision)
            for _, (reference, _) in sorted(self._buffers.items())
        )
        proxy_items = tuple(
            sorted(
                self._proxy_results.items(),
                key=lambda item: (item[0][0].value, item[0][1]),
            )
        )
        timings = tuple(self._tile_timings)
        return EngineDiagnostics(
            _process_memory_diagnostics(),
            H0_TILE_CACHE_BYTES,
            _swap_diagnostics(session),
            BufferInventory(
                entries,
                sum(item.extent.width * item.extent.height for item in entries),
            ),
            ProxyDiagnostics(
                tuple(sorted(key[1] for key, _ in proxy_items)),
                sum(
                    len(result.owned_bytes or b"")
                    for _, results in proxy_items
                    for result in results
                ),
                len(proxy_items),
            ),
            QueueDiagnostics(0, self._running_tiles),
            len(self._graphs),
            0,
            TimingDiagnostics(
                None if not timings else timings[-1],
                None if not timings else sum(timings) // len(timings),
                len(timings),
            ),
        )

    def export_tiles(
        self,
        requests: tuple[TileRequest, ...],
        *,
        cancel: CancelToken,
    ) -> tuple[TileResult, ...]:
        self._state()
        cancel.raise_if_cancelled()
        if not isinstance(requests, tuple) or not requests or any(
            not isinstance(item, TileRequest) for item in requests
        ):
            raise InvalidGraph("export requires non-empty immutable tile requests")
        if any(item.level != 0 for item in requests):
            raise InvalidGraph("full-resolution export must use level 0")
        identity = (
            requests[0].graph_digest,
            requests[0].spec,
            requests[0].revision,
        )
        if any(
            (item.graph_digest, item.spec, item.revision) != identity
            for item in requests
        ):
            raise InvalidGraph("export tiles do not share one immutable render identity")
        destinations = tuple(
            (
                item.destination.y,
                item.destination.x,
                item.destination.height,
                item.destination.width,
            )
            for item in requests
        )
        if destinations != tuple(sorted(set(destinations))):
            raise InvalidGraph("export tile destinations must be sorted and unique")
        completed: list[TileResult] = []
        for request in requests:
            cancel.raise_if_cancelled()
            completed.append(self.render_tile(request, cancel=cancel))
            cancel.raise_if_cancelled()
        cancel.raise_if_cancelled()
        return tuple(completed)

    def close(self) -> None:
        backend, _ = self._state()
        failure: Exception | None = None
        while self._proxies:
            _, proxy = self._proxies.popitem()
            try:
                backend.release_buffer(proxy)
            except Exception as exc:
                if failure is None:
                    failure = exc
            finally:
                del proxy
        while self._graphs:
            _, graph = self._graphs.popitem()
            try:
                backend.release_graph(graph)
            except Exception as exc:
                if failure is None:
                    failure = exc
            finally:
                del graph
        while self._buffers:
            _, (_, native) = self._buffers.popitem()
            try:
                backend.release_buffer(native)
            except Exception as exc:
                if failure is None:
                    failure = exc
            finally:
                del native
        gc.collect()
        self._mask_indexes.clear()
        self._proxy_results.clear()
        self._tile_timings.clear()
        self._plans.clear()
        self._graph_specs.clear()
        self._profiles.clear()
        self._capabilities = None
        try:
            self._runtime.close()
        except Exception as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            raise InternalEngineFailure(
                "OD-7 engine shutdown failed",
                diagnostic_ref="engine.shutdown",
            ) from failure


__all__ = (
    "BufferBinding",
    "CompiledGraphPlan",
    "CompiledPlanNode",
    "ImageRuntime",
    "NativeRuntimeBackend",
    "Od7ImageEngine",
    "PlanInput",
    "ProfileBinding",
    "RuntimeHandle",
    "RuntimeProcessGuard",
    "STARTUP_SEQUENCE",
    "StartupStep",
)
