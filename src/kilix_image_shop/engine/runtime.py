"""Guarded process-scoped GEGL/babl lifecycle.

This is the sole module allowed to import GI.  Imports occur inside the guarded
loader only after deterministic environment, carrier, and private-path checks.
"""

from __future__ import annotations

import fcntl
import gc
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import sys
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Protocol

from kilix_image_shop.domain.color import EngineCompatibility
from kilix_image_shop.domain.identifiers import ObjectId

from .api import (
    EngineCapabilities,
    GraphNodeKind,
    IncompatibleRuntime,
    InternalEngineFailure,
    InvalidGraph,
    PixelFormat,
    ResourceExhaustion,
    UnavailableGroup,
)
from .compatibility import (
    GI_ORIGIN,
    NativeObservation,
    RuntimeConfiguration,
    validate_native_observation,
)


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


__all__ = (
    "ImageRuntime",
    "NativeRuntimeBackend",
    "RuntimeHandle",
    "RuntimeProcessGuard",
    "STARTUP_SEQUENCE",
    "StartupStep",
)
