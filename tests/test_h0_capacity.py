from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "verify_h0_capacity.py"
SPEC = importlib.util.spec_from_file_location("f115_h0_capacity_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOOL
SPEC.loader.exec_module(TOOL)


def valid_observation():
    return TOOL.CapacityObservation(
        debian_version="13.6",
        architecture="x86_64",
        dmi_vendor="QEMU",
        dmi_product="Standard PC (Q35 + ICH9, 2009)",
        cpu_models=(TOOL.EXPECTED_CPU_MODEL, TOOL.EXPECTED_CPU_MODEL),
        effective_cpu_count=2,
        memory_total_kib=4_015_944,
        disk_bytes=40 * 1024 * 1024 * 1024,
        tmpdir_absolute=True,
        tmpdir_directory=True,
        tmpdir_symlink=False,
        tmpdir_mode=0o700,
        tmpdir_uid=1000,
        effective_uid=1000,
    )


class H0CapacityTests(unittest.TestCase):
    def test_complete_frozen_compute_envelope_passes_eight_of_eight(self) -> None:
        checks = TOOL.evaluate_capacity(valid_observation())
        self.assertEqual(len(checks), 8)
        self.assertEqual(sum(check.passed for check in checks), 8)

    def test_each_capacity_family_fails_closed(self) -> None:
        mutations = {
            "os": {"debian_version": "12.11"},
            "architecture": {"architecture": "aarch64"},
            "machine": {"dmi_product": "Standard PC (i440FX)"},
            "cpu_model": {"cpu_models": ("host", "host")},
            "cpu_topology": {"effective_cpu_count": 4},
            "memory": {"memory_total_kib": 3_899_999},
            "disk": {"disk_bytes": 39 * 1024 * 1024 * 1024},
            "tmpdir": {"tmpdir_mode": 0o755},
        }
        for expected_name, changes in mutations.items():
            with self.subTest(check=expected_name):
                observation = dataclasses.replace(valid_observation(), **changes)
                failed = {
                    check.name
                    for check in TOOL.evaluate_capacity(observation)
                    if not check.passed
                }
                self.assertIn(expected_name, failed)

    def test_proc_parsers_refuse_absent_ambiguous_or_wrong_unit_rows(self) -> None:
        self.assertEqual(TOOL.parse_memory_total_kib("MemTotal: 4015944 kB\n"), 4_015_944)
        for payload in (
            "",
            "MemTotal: 4015944 MB\n",
            "MemTotal: 1 kB\nMemTotal: 2 kB\n",
            "MemTotal: many kB\n",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(TOOL.CapacityRefusal):
                    TOOL.parse_memory_total_kib(payload)
        with self.assertRaises(TOOL.CapacityRefusal):
            TOOL.parse_cpu_models("processor: 0\n")

    def test_tool_contains_zero_private_paths_or_network_clients(self) -> None:
        source = TOOL_PATH.read_text()
        self.assertNotIn("/home/" + "pleb", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("requests", source)


if __name__ == "__main__":
    unittest.main()
