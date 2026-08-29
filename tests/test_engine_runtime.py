from __future__ import annotations

import ast
import dataclasses
import fcntl
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from engine_registry_fixtures import synthetic_registry
from kilix_image_shop.domain.color import (
    AlphaAssociation,
    ConversionPolicy,
    EngineCompatibility,
)
from kilix_image_shop.domain.identifiers import ObjectId
from kilix_image_shop.engine.api import (
    IncompatibleRuntime,
    InternalEngineFailure,
    InvalidGraph,
)
from kilix_image_shop.engine.compatibility import (
    BABL_NATIVE_VERSION,
    BABL_PACKAGE_VERSION,
    EXPECTED_OPERATION_COUNT,
    GEGL_NATIVE_VERSION,
    GEGL_PACKAGE_VERSION,
    GI_ORIGIN,
    H0_TILE_CACHE_BYTES,
    MINIMUM_OPERATIONS,
    NativeObservation,
    OperationRegistry,
    PACKAGE_GROUP_ID,
    PYTHON_GI_PACKAGE_VERSION,
    RuntimeConfiguration,
    compatibility_differences,
    require_compatible,
)
from kilix_image_shop.engine.runtime import (
    STARTUP_SEQUENCE,
    ImageRuntime,
    RuntimeProcessGuard,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE = ObjectId("1" * 64)
GI_DIGEST = ObjectId("3" * 64)
PLUGIN_BYTES = b'{"schema":"synthetic-plugin-tree/v1"}\n'
GROUP_BYTES = b'{"group":"plebian.f115.image-engine","synthetic":true}\n'


def operation_population() -> tuple[str, ...]:
    native = set(synthetic_registry().native_operations)
    extras = tuple(
        f"gegl:synthetic-{index:03d}"
        for index in range(EXPECTED_OPERATION_COUNT - len(native))
    )
    result = tuple(sorted((*native, *extras)))
    assert len(result) == EXPECTED_OPERATION_COUNT
    return result


def compatibility(
    *,
    group_digest: ObjectId,
    plugin_digest: ObjectId,
    registry: OperationRegistry,
) -> EngineCompatibility:
    return EngineCompatibility(
        schema=EngineCompatibility.SCHEMA,
        package_group_id=PACKAGE_GROUP_ID,
        package_group_digest=group_digest,
        gegl_version=GEGL_PACKAGE_VERSION,
        babl_version=BABL_PACKAGE_VERSION,
        python_gi_version=PYTHON_GI_PACKAGE_VERSION,
        gi_file_digest=GI_DIGEST,
        operation_count=EXPECTED_OPERATION_COUNT,
        operation_set_digest=registry.digest,
        plugin_tree_digest=plugin_digest,
        working_format="RGBA u16",
        alpha_association=AlphaAssociation.STRAIGHT,
        mask_format="Y u8",
        mask_semantics="foreground-alpha",
        working_profile=PROFILE,
        conversion_policy=ConversionPolicy.RELATIVE_COLORIMETRIC,
        resampling_kernel="synthetic-nohalo",
        edge_mode="synthetic-clamp",
        tile_halos=(("synthetic-default", 0),),
        use_opencl=False,
        tile_cache_bytes=H0_TILE_CACHE_BYTES,
        swap_compression="fast",
        threads=2,
        deterministic_preset="f115-synthetic-h0-u16-v1",
        babl_tolerance="0.0",
    )


def observation(*, operations: tuple[str, ...] | None = None) -> NativeObservation:
    return NativeObservation(
        gegl_native_version=GEGL_NATIVE_VERSION,
        babl_native_version=BABL_NATIVE_VERSION,
        gegl_package_version=GEGL_PACKAGE_VERSION,
        babl_package_version=BABL_PACKAGE_VERSION,
        python_gi_package_version=PYTHON_GI_PACKAGE_VERSION,
        gi_origin=GI_ORIGIN,
        gi_file_digest=GI_DIGEST,
        operations=operation_population() if operations is None else operations,
    )


class FakeNativeBackend:
    def __init__(self, identity: NativeObservation | None = None) -> None:
        self.identity = observation() if identity is None else identity
        self.events: list[str] = []
        self.values: dict[str, object] = {}
        self.readback_override: dict[str, object] | None = None
        self.smoke_failure: Exception | None = None
        self.shutdown_failure: Exception | None = None

    def initialize(self) -> None:
        self.events.append("initialize")

    def configure(self, values: tuple[tuple[str, object], ...]) -> None:
        self.events.append("configure")
        self.values = dict(values)

    def read_configuration(self, names: tuple[str, ...]) -> dict[str, object]:
        self.events.append("read")
        if self.readback_override is not None:
            return self.readback_override
        return {name: self.values[name] for name in names}

    def observe(self) -> NativeObservation:
        self.events.append("observe")
        return self.identity

    def verify_operation_registry(self, registry: OperationRegistry) -> None:
        self.events.append("registry")
        if registry.digest != synthetic_registry().digest:
            raise IncompatibleRuntime("synthetic operation registry differs")

    def smoke_test(self) -> None:
        self.events.append("smoke")
        if self.smoke_failure is not None:
            raise self.smoke_failure

    def shutdown(self) -> None:
        self.events.append("shutdown")
        if self.shutdown_failure is not None:
            raise self.shutdown_failure


class RuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kilix-runtime-test-")
        self.root = pathlib.Path(self.temporary.name)
        self.group_record = self.root / "group.json"
        self.plugin_manifest = self.root / "plugins.json"
        self.group_record.write_bytes(GROUP_BYTES)
        self.plugin_manifest.write_bytes(PLUGIN_BYTES)
        self.registry = synthetic_registry()
        self.expected = compatibility(
            group_digest=ObjectId.from_bytes(GROUP_BYTES),
            plugin_digest=ObjectId.from_bytes(PLUGIN_BYTES),
            registry=self.registry,
        )
        self.configuration = RuntimeConfiguration(
            expected=self.expected,
            operation_registry=self.registry,
            package_group_record=self.group_record,
            plugin_tree_manifest=self.plugin_manifest,
            cache_root=self.root / "cache",
            runtime_root=self.root / "runtime",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def runtime(
        self,
        backend: FakeNativeBackend | None = None,
        *,
        configuration: RuntimeConfiguration | None = None,
        guard: RuntimeProcessGuard | None = None,
    ) -> tuple[ImageRuntime, FakeNativeBackend, list[str]]:
        selected = FakeNativeBackend() if backend is None else backend
        loader_events: list[str] = []

        def load() -> FakeNativeBackend:
            loader_events.append("load")
            return selected

        return (
            ImageRuntime(
                self.configuration if configuration is None else configuration,
                native_loader=load,
                process_guard=RuntimeProcessGuard() if guard is None else guard,
            ),
            selected,
            loader_events,
        )

    def start(self, runtime: ImageRuntime):
        with mock.patch.dict(
            os.environ,
            {"BABL_TOLERANCE": "0.0"},
            clear=True,
        ):
            return runtime.start()


class ConfigurationTests(RuntimeFixture):
    def test_h0_configuration_is_fully_explicit(self) -> None:
        self.assertEqual(self.expected.package_group_id, PACKAGE_GROUP_ID)
        self.assertEqual(self.expected.operation_count, 203)
        self.assertEqual(self.expected.tile_cache_bytes, 268435456)
        self.assertEqual(self.expected.threads, 2)
        self.assertFalse(self.expected.use_opencl)
        self.assertTrue(self.configuration.mipmap_rendering)

    def test_operation_registry_is_canonical_complete_and_digest_bound(self) -> None:
        self.assertEqual(len(MINIMUM_OPERATIONS), 11)
        self.assertEqual(len(self.registry.definitions), 46)
        self.assertEqual(len({item.family for item in self.registry.definitions}), 8)
        self.assertEqual(self.registry.digest, self.expected.operation_set_digest)
        self.assertEqual(
            OperationRegistry.from_bytes(self.registry.canonical_bytes()),
            self.registry,
        )
        duplicate = self.registry.canonical_bytes().replace(
            b'"schema":', b'"schema":"duplicate","schema":', 1
        )
        with self.assertRaises(InvalidGraph):
            OperationRegistry.from_bytes(duplicate)
        with self.assertRaises(InvalidGraph):
            RuntimeConfiguration(
                expected=dataclasses.replace(
                    self.expected,
                    operation_set_digest=ObjectId("0" * 64),
                ),
                operation_registry=self.registry,
                package_group_record=self.group_record,
                plugin_tree_manifest=self.plugin_manifest,
                cache_root=self.root / "cache-2",
                runtime_root=self.root / "runtime-2",
            )

    def test_h0_refuses_float_opencl_default_threads_and_unfixed_carriers(self) -> None:
        replacements = (
            {"working_format": "RGBA float"},
            {"use_opencl": True},
            {"tile_cache_bytes": 1},
            {"swap_compression": "zlib"},
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement), self.assertRaises(InvalidGraph):
                RuntimeConfiguration(
                    expected=dataclasses.replace(self.expected, **replacement),
                    operation_registry=self.registry,
                    package_group_record=self.group_record,
                    plugin_tree_manifest=self.plugin_manifest,
                    cache_root=self.root / "cache-3",
                    runtime_root=self.root / "runtime-3",
                )

    def test_compatibility_comparison_is_exact_and_field_named(self) -> None:
        require_compatible(self.expected, self.expected)
        changed = dataclasses.replace(self.expected, threads=3, edge_mode="other")
        differences = compatibility_differences(self.expected, changed)
        self.assertEqual(tuple(item.field for item in differences), ("edgeMode", "threads"))
        with self.assertRaisesRegex(IncompatibleRuntime, "edgeMode,threads"):
            require_compatible(self.expected, changed)


class StartupTests(RuntimeFixture):
    def test_ten_step_startup_publishes_only_after_smoke(self) -> None:
        runtime, backend, loader_events = self.runtime()
        handle = self.start(runtime)
        self.assertEqual(handle.completed_steps, STARTUP_SEQUENCE)
        self.assertEqual(len(STARTUP_SEQUENCE), 10)
        self.assertEqual(loader_events, ["load"])
        self.assertEqual(
            backend.events,
            ["initialize", "configure", "read", "observe", "registry", "smoke"],
        )
        self.assertEqual(
            backend.values,
            {
                "tile-cache-size": 268435456,
                "use-opencl": False,
                "swap": str(handle.swap_directory),
                "swap-compression": "fast",
                "mipmap-rendering": True,
                "threads": 2,
            },
        )
        self.assertEqual(handle.capabilities.compatibility_digest, self.expected.digest)
        self.assertEqual(handle.compatibility, self.expected)
        self.assertTrue(handle.swap_directory.is_dir())
        self.assertEqual(stat.S_IMODE(handle.swap_directory.stat().st_mode), 0o700)
        runtime.close()
        self.assertFalse(handle.swap_directory.exists())
        self.assertEqual(backend.events[-1], "shutdown")

    def test_readback_must_match_all_six_native_properties_exactly(self) -> None:
        backend = FakeNativeBackend()
        backend.readback_override = {
            "tile-cache-size": 268435456,
            "use-opencl": False,
            "swap": "wrong",
            "swap-compression": "fast",
            "mipmap-rendering": True,
            "threads": 2,
        }
        runtime, _, _ = self.runtime(backend)
        with self.assertRaises(IncompatibleRuntime):
            self.start(runtime)
        self.assertIsNone(runtime.handle)
        self.assertEqual(backend.events[-1], "shutdown")
        self.assertEqual(
            tuple(
                (self.root / "cache/kilix-image-shop/gegl-swap").glob("session-*")
            ),
            (),
        )

    def test_environment_and_early_import_refuse_before_loader(self) -> None:
        runtime, _, loader_events = self.runtime()
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
            IncompatibleRuntime
        ):
            runtime.start()
        self.assertEqual(loader_events, [])

        runtime, _, loader_events = self.runtime()
        with (
            mock.patch.dict(os.environ, {"BABL_TOLERANCE": "0.0"}, clear=True),
            mock.patch.dict(sys.modules, {"gi.repository.Gegl": object()}),
            self.assertRaises(IncompatibleRuntime),
        ):
            runtime.start()
        self.assertEqual(loader_events, [])

    def test_pythonpath_is_refused_even_under_isolated_python(self) -> None:
        runtime, _, loader_events = self.runtime()
        with (
            mock.patch.dict(
                os.environ,
                {"BABL_TOLERANCE": "0.0", "PYTHONPATH": "/untrusted"},
                clear=True,
            ),
            self.assertRaises(IncompatibleRuntime),
        ):
            runtime.start()
        self.assertEqual(loader_events, [])

    def test_carrier_digest_mismatch_refuses_before_paths_and_loader(self) -> None:
        malformed = RuntimeConfiguration(
            expected=dataclasses.replace(
                self.expected,
                package_group_digest=ObjectId("9" * 64),
            ),
            operation_registry=self.registry,
            package_group_record=self.group_record,
            plugin_tree_manifest=self.plugin_manifest,
            cache_root=self.root / "unused-cache",
            runtime_root=self.root / "unused-runtime",
        )
        runtime, _, loader_events = self.runtime(configuration=malformed)
        with self.assertRaises(IncompatibleRuntime):
            self.start(runtime)
        self.assertEqual(loader_events, [])
        self.assertFalse(malformed.cache_root.exists())

    def test_runtime_identity_checks_versions_origin_digest_count_and_required_set(self) -> None:
        variants = (
            dataclasses.replace(observation(), gegl_native_version="0.4.61"),
            dataclasses.replace(observation(), babl_package_version="0.1.0"),
            dataclasses.replace(observation(), gi_origin=pathlib.Path("/tmp/gi.py")),
            dataclasses.replace(observation(), gi_file_digest=ObjectId("8" * 64)),
            dataclasses.replace(observation(), operations=operation_population()[:-1]),
            dataclasses.replace(
                observation(),
                operations=tuple(
                    item
                    for item in operation_population()
                    if item != "gegl:write-buffer"
                ),
            ),
        )
        for index, identity in enumerate(variants):
            with self.subTest(index=index):
                configuration = dataclasses.replace(
                    self.configuration,
                    cache_root=self.root / f"cache-identity-{index}",
                    runtime_root=self.root / f"runtime-identity-{index}",
                )
                runtime, _, _ = self.runtime(
                    FakeNativeBackend(identity),
                    configuration=configuration,
                )
                with self.assertRaises(IncompatibleRuntime):
                    self.start(runtime)
                self.assertIsNone(runtime.handle)

    def test_smoke_failure_is_private_and_publishes_no_handle(self) -> None:
        backend = FakeNativeBackend()
        backend.smoke_failure = RuntimeError("native detail must stay private")
        runtime, _, _ = self.runtime(backend)
        with self.assertRaises(InternalEngineFailure) as caught:
            self.start(runtime)
        self.assertNotIn("native detail", str(caught.exception))
        self.assertEqual(caught.exception.diagnostic_ref, "runtime.initialization")
        self.assertIsNone(runtime.handle)

    def test_process_guard_forbids_restart_after_close(self) -> None:
        guard = RuntimeProcessGuard()
        runtime, _, _ = self.runtime(guard=guard)
        self.start(runtime)
        runtime.close()
        self.assertEqual(guard.state, "closed")
        second, _, loader_events = self.runtime(guard=guard)
        with self.assertRaises(IncompatibleRuntime):
            self.start(second)
        self.assertEqual(loader_events, [])

    def test_runtime_lifecycle_is_owned_by_one_thread(self) -> None:
        runtime, _, _ = self.runtime()
        self.start(runtime)
        failures: list[BaseException] = []

        def close_from_non_owner() -> None:
            try:
                runtime.close()
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=close_from_non_owner)
        worker.start()
        worker.join()
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], InternalEngineFailure)
        runtime.close()


class SwapOwnershipTests(RuntimeFixture):
    def _stale_session(self, token: str, *, marked: bool = True) -> pathlib.Path:
        swap_root = self.configuration.cache_root / "kilix-image-shop/gegl-swap"
        swap_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.configuration.cache_root / "kilix-image-shop", 0o700)
        os.chmod(swap_root, 0o700)
        session = swap_root / f"session-{token}"
        session.mkdir(mode=0o700)
        (session / "LOCK").write_bytes(b"")
        if marked:
            (session / "OWNER.json").write_text(
                json.dumps(
                    {
                        "schema": "kilix.imageshop.gegl-swap-session/v1",
                        "token": token,
                        "uid": os.getuid(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
        return session

    def test_only_marked_unlocked_stale_sessions_are_removed(self) -> None:
        stale = self._stale_session("a" * 32)
        unmarked = self._stale_session("b" * 32, marked=False)
        runtime, _, _ = self.runtime()
        self.start(runtime)
        self.assertFalse(stale.exists())
        self.assertTrue(unmarked.exists())
        runtime.close()

    def test_live_locked_session_is_not_removed(self) -> None:
        live = self._stale_session("c" * 32)
        descriptor = os.open(live / "LOCK", os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            runtime, _, _ = self.runtime()
            self.start(runtime)
            self.assertTrue(live.exists())
            runtime.close()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            shutil.rmtree(live)


class NativeImportBoundaryTests(unittest.TestCase):
    def test_runtime_owns_the_only_two_gi_import_statements(self) -> None:
        statements: list[tuple[str, str]] = []
        engine_root = ROOT / "src/kilix_image_shop/engine"
        for path in sorted(engine_root.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "gi" or alias.name.startswith("gi."):
                            statements.append((path.name, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == "gi" or module.startswith("gi."):
                        statements.append((path.name, module))
        self.assertEqual(
            statements,
            [("runtime.py", "gi"), ("runtime.py", "gi.repository")],
        )

    def test_importing_engine_package_eagerly_loads_zero_gi_modules(self) -> None:
        self.assertNotIn("gi", sys.modules)
        self.assertNotIn("gi.repository.Gegl", sys.modules)
        self.assertNotIn("gi.repository.Babl", sys.modules)


if __name__ == "__main__":
    unittest.main()
