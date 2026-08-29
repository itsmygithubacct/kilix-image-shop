from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools/verify_f115_qualification.py"
SPEC = importlib.util.spec_from_file_location("f115_qualification_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOOL
SPEC.loader.exec_module(TOOL)


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def reference(path: str, payload: bytes) -> dict[str, object]:
    return {
        "path": path,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def fixture(fixture_id: str) -> dict[str, object]:
    direct = {"value": "synthetic", "source": "unit-fixture"}
    return {
        "schema": "kilix.f115.fixture-manifest/v1",
        "fixture": {"id": fixture_id, "class": direct, "platform": direct},
        "cpu": {"identity": direct, "isa": direct, "topology": direct},
        "firmware": direct,
        "installer": direct,
        "os": direct,
        "apt": direct,
        "memory": {"system": direct, "cgroup": direct, "swap": direct},
        "storage": {"target": direct, "gegl": direct},
        "graphics": direct,
        "engine": {"opencl": direct, "identity": direct},
        "power_thermal": direct,
        "python_tool": direct,
        "packages": direct,
        "campaign": {"inputs": direct},
        "run_environment": direct,
        "freeze": direct,
    }


class QualificationHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kilix-qualification-test-")
        self.root = pathlib.Path(self.temporary.name)
        self.original_input_digest = TOOL.INPUT_100MP_SHA256

    def tearDown(self) -> None:
        TOOL.INPUT_100MP_SHA256 = self.original_input_digest
        self.temporary.cleanup()

    def write(self, relative: str, payload: bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def build_valid_root(self) -> tuple[str, str]:
        primary = json_bytes(fixture("primary-fixture"))
        comparator = json_bytes(fixture("comparator-fixture"))
        package = json_bytes(
            {
                "schema": "kilix.f115.package-group-input/v1",
                "release": {"id": "0.2.1", "architecture": "amd64"},
                "repository": {"snapshot": "synthetic"},
                "direct": [{"name": f"package-{index}"} for index in range(11)],
                "closure": {"sha256": "1" * 64},
                "partitions": {"complete": True},
                "sizes": {"complete": True},
                "licensing": {"complete": True},
                "exclusions": {"complete": True},
                "services": {"complete": True},
                "reboot": {"required": False},
                "stateChanges": {"complete": True},
                "references": {"complete": True},
                "removal": {"complete": True},
                "lifecycle": {"complete": True},
            }
        )
        resource = b"#!/bin/sh\nexit 0\n"
        self.write("harness/runner.sh", resource)
        harness = json_bytes(
            {
                "schema": "kilix.f115.harness-manifest/v1",
                "resources": [reference("harness/runner.sh", resource)],
                "commands": [
                    {"group": item, "argv": ["harness/runner.sh"]}
                    for item in TOOL.CAMPAIGN_GROUPS
                ],
                "corrections": {item: True for item in TOOL.HARNESS_CORRECTIONS},
                "negativeProcessorEvidence": list(TOOL.NEGATIVE_PROCESSOR_EVIDENCE),
                "campaignGroups": list(TOOL.CAMPAIGN_GROUPS),
            }
        )
        input_payload = b"synthetic 100 MP identity carrier"
        input_digest = hashlib.sha256(input_payload).hexdigest()
        TOOL.INPUT_100MP_SHA256 = input_digest
        carriers = {
            "fixtures/h0-primary/fixture-manifest.json": primary,
            "fixtures/determinism-comparator/fixture-manifest.json": comparator,
            "packages/f115-package-group-input.json": package,
            "harness/harness-manifest.json": harness,
            "inputs/source-100mp.bin": input_payload,
        }
        for path, payload in carriers.items():
            self.write(path, payload)
        fixture_set = {
            "schema": "kilix.f115.fixture-set/v1",
            "release": {"id": "0.2.1", "stream": "F115"},
            "frozenAt": "2026-08-29T00:00:00Z",
            "owner": "release-owner",
            "primary": reference(
                "fixtures/h0-primary/fixture-manifest.json",
                primary,
            ),
            "comparator": reference(
                "fixtures/determinism-comparator/fixture-manifest.json",
                comparator,
            ),
            "packageInput": reference(
                "packages/f115-package-group-input.json",
                package,
            ),
            "commonGroup": {
                "schema": "kilix.lazy-package-group/v1",
                "recordId": "plebian.f115.image-engine",
                "sha256": "2" * 64,
            },
            "harness": reference("harness/harness-manifest.json", harness),
            "input100mp": reference("inputs/source-100mp.bin", input_payload),
            "environment": {"presetSha256": "3" * 64},
            "campaign": {"runSetSha256": "4" * 64, "disposition": "frozen"},
        }
        self.write("fixture-set.json", json_bytes(fixture_set))
        return hashlib.sha256(primary).hexdigest(), hashlib.sha256(comparator).hexdigest()

    def verify(self):
        primary_digest, comparator_digest = self.build_valid_root()
        return TOOL.verify_evidence_root(
            self.root,
            primary_id="primary-fixture",
            primary_manifest_sha256=primary_digest,
            comparator_id="comparator-fixture",
            comparator_manifest_sha256=comparator_digest,
        )

    def test_complete_synthetic_packet_binds_every_frozen_population(self) -> None:
        report = self.verify()
        self.assertEqual((report.carriers_verified, report.carriers_total), (5, 5))
        self.assertEqual((report.roles_verified, report.roles_total), (2, 2))
        self.assertEqual(
            (report.fixture_fields_verified, report.fixture_fields_total),
            (48, 48),
        )
        self.assertEqual(
            (report.package_fields_verified, report.package_fields_total),
            (15, 15),
        )
        self.assertEqual(
            (report.harness_corrections_verified, report.harness_corrections_total),
            (8, 8),
        )
        self.assertEqual(
            (report.campaign_groups_bound, report.campaign_groups_total),
            (8, 8),
        )
        self.assertTrue(report.frozen_disposition)

    def test_changed_carrier_is_refused_before_any_qualification_claim(self) -> None:
        primary_digest, comparator_digest = self.build_valid_root()
        path = self.root / "fixtures/h0-primary/fixture-manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(TOOL.QualificationRefusal, "byte count differs"):
            TOOL.verify_evidence_root(
                self.root,
                primary_id="primary-fixture",
                primary_manifest_sha256=primary_digest,
                comparator_id="comparator-fixture",
                comparator_manifest_sha256=comparator_digest,
            )

    def test_duplicate_members_and_symlink_resources_are_refused(self) -> None:
        with self.assertRaisesRegex(TOOL.QualificationRefusal, "repeats"):
            TOOL._strict_json(b'{"schema":1,"schema":2}\n')
        primary_digest, comparator_digest = self.build_valid_root()
        resource = self.root / "harness/runner.sh"
        outside = self.root / "outside.sh"
        outside.write_bytes(resource.read_bytes())
        resource.unlink()
        resource.symlink_to(outside)
        with self.assertRaisesRegex(TOOL.QualificationRefusal, "opened safely"):
            TOOL.verify_evidence_root(
                self.root,
                primary_id="primary-fixture",
                primary_manifest_sha256=primary_digest,
                comparator_id="comparator-fixture",
                comparator_manifest_sha256=comparator_digest,
            )

    def test_absent_owner_packet_reports_blocked_not_passed(self) -> None:
        with self.assertRaises(TOOL.QualificationRefusal):
            TOOL.verify_evidence_root(
                self.root,
                primary_id="missing-primary",
                primary_manifest_sha256="1" * 64,
                comparator_id="missing-comparator",
                comparator_manifest_sha256="2" * 64,
            )

    def test_tool_embeds_zero_private_paths_and_zero_network_clients(self) -> None:
        source = TOOL_PATH.read_text()
        self.assertNotIn("/home/" + "pleb", source)
        self.assertNotIn("/var/" + "tmp", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("requests", source)


if __name__ == "__main__":
    unittest.main()
