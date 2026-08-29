#!/usr/bin/env python3
"""Fail-closed binder for owner-frozen F115 qualification carriers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
from dataclasses import asdict, dataclass


INPUT_100MP_SHA256 = "95954aac51e62ff9c66b3e44e52d1508345183e9fa23ff8655000b2a6f922233"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_JSON_BYTES = 16 * 1024 * 1024
FIXTURE_POINTERS: tuple[tuple[str, ...], ...] = (
    ("fixture", "id"),
    ("fixture", "class"),
    ("fixture", "platform"),
    ("cpu", "identity"),
    ("cpu", "isa"),
    ("firmware",),
    ("cpu", "topology"),
    ("installer",),
    ("os",),
    ("apt",),
    ("memory", "system"),
    ("memory", "cgroup"),
    ("memory", "swap"),
    ("storage", "target"),
    ("storage", "gegl"),
    ("graphics",),
    ("engine", "opencl"),
    ("power_thermal",),
    ("python_tool",),
    ("packages",),
    ("engine", "identity"),
    ("campaign", "inputs"),
    ("run_environment",),
    ("freeze",),
)
PACKAGE_FIELDS = {
    "schema",
    "release",
    "repository",
    "direct",
    "closure",
    "partitions",
    "sizes",
    "licensing",
    "exclusions",
    "services",
    "reboot",
    "stateChanges",
    "references",
    "removal",
    "lifecycle",
}
HARNESS_CORRECTIONS = {
    "argumentPaths",
    "resourceIdentities",
    "boundedWaits",
    "installedGroup",
    "inputIdentity",
    "runState",
    "atomicPublication",
    "isolatedSelfTest",
}
CAMPAIGN_GROUPS = (
    "format-cache",
    "proxy-session",
    "tile-latency",
    "swap-modes",
    "mask-strokes",
    "gi-boundary",
    "thread-scaling",
    "determinism",
)
NEGATIVE_PROCESSOR_EVIDENCE = (
    "m7-bounded.py",
    "m7-cancel.py",
    "m7-granularity.py",
)
FIXTURE_SET_FIELDS = {
    "schema",
    "release",
    "frozenAt",
    "owner",
    "primary",
    "comparator",
    "packageInput",
    "commonGroup",
    "harness",
    "input100mp",
    "environment",
    "campaign",
}


class QualificationRefusal(ValueError):
    """A carrier is absent, malformed, unsafe, or differs from frozen identity."""


@dataclass(frozen=True, slots=True)
class CarrierReference:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class QualificationReport:
    carriers_verified: int
    carriers_total: int
    roles_verified: int
    roles_total: int
    fixture_fields_verified: int
    fixture_fields_total: int
    package_fields_verified: int
    package_fields_total: int
    harness_corrections_verified: int
    harness_corrections_total: int
    campaign_groups_bound: int
    campaign_groups_total: int
    input_identities_verified: int
    input_identities_total: int
    frozen_disposition: bool


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationRefusal("JSON carrier repeats an object member")
        result[key] = value
    return result


def _relative_path(value: object) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise QualificationRefusal("carrier path is not normalized relative POSIX")
    parsed = pathlib.PurePosixPath(value)
    if parsed.is_absolute() or value != parsed.as_posix() or any(
        part in {"", ".", ".."} for part in parsed.parts
    ):
        raise QualificationRefusal("carrier path is not normalized relative POSIX")
    return parsed


def _safe_path(root: pathlib.Path, relative: pathlib.PurePosixPath) -> pathlib.Path:
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise QualificationRefusal("carrier parent is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise QualificationRefusal("carrier parent is not a real directory")
    return candidate


def _read_regular(path: pathlib.Path, maximum: int | None = None) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise QualificationRefusal("carrier is not a regular file")
        if metadata.st_size <= 0 or (maximum is not None and metadata.st_size > maximum):
            raise QualificationRefusal("carrier byte count is outside its bound")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise QualificationRefusal("carrier ended before its recorded size")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise QualificationRefusal("carrier grew while being verified")
        return b"".join(chunks)
    except OSError as exc:
        raise QualificationRefusal("carrier cannot be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _hash_regular(path: pathlib.Path) -> tuple[int, str]:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise QualificationRefusal("carrier is not a non-empty regular file")
        digest = hashlib.sha256()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise QualificationRefusal("carrier ended before its recorded size")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise QualificationRefusal("carrier grew while being verified")
        return metadata.st_size, digest.hexdigest()
    except OSError as exc:
        raise QualificationRefusal("carrier cannot be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _strict_json(payload: bytes) -> dict[str, object]:
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise QualificationRefusal("JSON carrier must end in exactly one LF")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationRefusal("carrier is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise QualificationRefusal("carrier root must be an object")
    return value


def _reference(value: object) -> CarrierReference:
    if not isinstance(value, dict) or set(value) != {"path", "bytes", "sha256"}:
        raise QualificationRefusal("carrier reference has missing or unknown fields")
    _relative_path(value["path"])
    byte_count = value["bytes"]
    digest = value["sha256"]
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count <= 0
        or not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
    ):
        raise QualificationRefusal("carrier reference identity is malformed")
    return CarrierReference(value["path"], byte_count, digest)


def _verify_reference(
    root: pathlib.Path,
    reference: CarrierReference,
    *,
    json_carrier: bool,
) -> tuple[bytes, dict[str, object] | None]:
    path = _safe_path(root, _relative_path(reference.path))
    if not json_carrier:
        byte_count, digest = _hash_regular(path)
        if byte_count != reference.bytes:
            raise QualificationRefusal(
                "carrier byte count differs from its frozen reference"
            )
        if digest != reference.sha256:
            raise QualificationRefusal("carrier digest differs from its frozen reference")
        return b"", None
    payload = _read_regular(path, MAX_JSON_BYTES)
    if len(payload) != reference.bytes:
        raise QualificationRefusal("carrier byte count differs from its frozen reference")
    if hashlib.sha256(payload).hexdigest() != reference.sha256:
        raise QualificationRefusal("carrier digest differs from its frozen reference")
    return payload, _strict_json(payload)


def _pointer(value: dict[str, object], path: tuple[str, ...]) -> object:
    current: object = value
    for component in path:
        if not isinstance(current, dict) or component not in current:
            raise QualificationRefusal("fixture manifest omits a required direct pointer")
        current = current[component]
    if current is None or current == "" or current == {} or current == []:
        raise QualificationRefusal("fixture manifest direct pointer is empty")
    return current


def _verify_fixture(
    value: dict[str, object],
    *,
    expected_id: str,
    expected_digest: str,
    actual_digest: str,
) -> None:
    if value.get("schema") != "kilix.f115.fixture-manifest/v1":
        raise QualificationRefusal("fixture manifest schema is unsupported")
    for pointer in FIXTURE_POINTERS:
        _pointer(value, pointer)
    if _pointer(value, ("fixture", "id")) != expected_id:
        raise QualificationRefusal("fixture ID differs from its owner freeze token")
    if actual_digest != expected_digest:
        raise QualificationRefusal("fixture digest differs from its owner freeze token")


def _verify_package(value: dict[str, object]) -> None:
    if set(value) != PACKAGE_FIELDS:
        raise QualificationRefusal("package input has missing or unknown semantic fields")
    if value.get("schema") != "kilix.f115.package-group-input/v1":
        raise QualificationRefusal("package input schema is unsupported")
    if any(value[field] is None for field in PACKAGE_FIELDS):
        raise QualificationRefusal("frozen package input contains an open field")
    direct = value["direct"]
    if not isinstance(direct, list) or len(direct) != 11:
        raise QualificationRefusal("package input does not contain 11 direct rows")


def _verify_harness(root: pathlib.Path, value: dict[str, object]) -> None:
    required = {
        "schema",
        "resources",
        "commands",
        "corrections",
        "negativeProcessorEvidence",
        "campaignGroups",
    }
    if set(value) != required or value.get("schema") != "kilix.f115.harness-manifest/v1":
        raise QualificationRefusal("harness manifest shape or schema is unsupported")
    corrections = value["corrections"]
    if not isinstance(corrections, dict) or set(corrections) != HARNESS_CORRECTIONS:
        raise QualificationRefusal("harness does not bind all eight corrections")
    if any(item is not True for item in corrections.values()):
        raise QualificationRefusal("harness correction is not closed")
    groups = value["campaignGroups"]
    if not isinstance(groups, list) or tuple(groups) != CAMPAIGN_GROUPS:
        raise QualificationRefusal("harness campaign population differs from 8/8")
    negative = value["negativeProcessorEvidence"]
    if not isinstance(negative, list) or tuple(negative) != NEGATIVE_PROCESSOR_EVIDENCE:
        raise QualificationRefusal("harness negative processor evidence differs from 3/3")
    resources = value["resources"]
    if not isinstance(resources, list) or not resources:
        raise QualificationRefusal("harness resource population is empty")
    for item in resources:
        _verify_reference(root, _reference(item), json_carrier=False)
    commands = value["commands"]
    if not isinstance(commands, list) or len(commands) != len(CAMPAIGN_GROUPS):
        raise QualificationRefusal("harness command population differs from 8/8")


def verify_evidence_root(
    evidence_root: pathlib.Path,
    *,
    primary_id: str,
    primary_manifest_sha256: str,
    comparator_id: str,
    comparator_manifest_sha256: str,
) -> QualificationReport:
    if not isinstance(evidence_root, pathlib.Path) or not evidence_root.is_absolute():
        raise QualificationRefusal("evidence root must be an explicit absolute path")
    try:
        metadata = evidence_root.lstat()
    except OSError as exc:
        raise QualificationRefusal("evidence root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise QualificationRefusal("evidence root must be a real directory")
    for digest in (primary_manifest_sha256, comparator_manifest_sha256):
        if SHA256_RE.fullmatch(digest) is None:
            raise QualificationRefusal("owner freeze token digest is malformed")

    fixture_set = _strict_json(
        _read_regular(evidence_root / "fixture-set.json", MAX_JSON_BYTES)
    )
    if set(fixture_set) != FIXTURE_SET_FIELDS:
        raise QualificationRefusal("fixture set has missing or unknown fields")
    if fixture_set.get("schema") != "kilix.f115.fixture-set/v1":
        raise QualificationRefusal("fixture-set schema is unsupported")
    release = fixture_set["release"]
    if release != {"id": "0.2.1", "stream": "F115"}:
        raise QualificationRefusal("fixture set release identity differs")
    for name in ("frozenAt", "owner"):
        if not isinstance(fixture_set[name], str) or not fixture_set[name]:
            raise QualificationRefusal("fixture set freeze identity is empty")

    primary_ref = _reference(fixture_set["primary"])
    comparator_ref = _reference(fixture_set["comparator"])
    package_ref = _reference(fixture_set["packageInput"])
    harness_ref = _reference(fixture_set["harness"])
    _, primary = _verify_reference(evidence_root, primary_ref, json_carrier=True)
    _, comparator = _verify_reference(evidence_root, comparator_ref, json_carrier=True)
    _, package = _verify_reference(evidence_root, package_ref, json_carrier=True)
    _, harness = _verify_reference(evidence_root, harness_ref, json_carrier=True)
    assert primary is not None and comparator is not None
    assert package is not None and harness is not None
    _verify_fixture(
        primary,
        expected_id=primary_id,
        expected_digest=primary_manifest_sha256,
        actual_digest=primary_ref.sha256,
    )
    _verify_fixture(
        comparator,
        expected_id=comparator_id,
        expected_digest=comparator_manifest_sha256,
        actual_digest=comparator_ref.sha256,
    )
    _verify_package(package)
    _verify_harness(evidence_root, harness)

    input_ref = _reference(fixture_set["input100mp"])
    _verify_reference(evidence_root, input_ref, json_carrier=False)
    if input_ref.sha256 != INPUT_100MP_SHA256:
        raise QualificationRefusal("100 MP input differs from its frozen identity")
    common_group = fixture_set["commonGroup"]
    if not isinstance(common_group, dict) or set(common_group) != {
        "schema",
        "recordId",
        "sha256",
    }:
        raise QualificationRefusal("common group identity is incomplete")
    if not all(
        isinstance(common_group[item], str) and common_group[item]
        for item in common_group
    ):
        raise QualificationRefusal("common group identity contains an empty value")
    if SHA256_RE.fullmatch(common_group["sha256"]) is None:
        raise QualificationRefusal("common group digest is malformed")
    environment = fixture_set["environment"]
    if not isinstance(environment, dict) or set(environment) != {"presetSha256"}:
        raise QualificationRefusal("normalized environment identity is incomplete")
    preset_digest = environment["presetSha256"]
    if not isinstance(preset_digest, str) or SHA256_RE.fullmatch(preset_digest) is None:
        raise QualificationRefusal("normalized environment digest is malformed")
    campaign = fixture_set["campaign"]
    if not isinstance(campaign, dict) or set(campaign) != {
        "runSetSha256",
        "disposition",
    }:
        raise QualificationRefusal("campaign identity is incomplete")
    run_set_digest = campaign["runSetSha256"]
    if not isinstance(run_set_digest, str) or SHA256_RE.fullmatch(run_set_digest) is None:
        raise QualificationRefusal("campaign run-set digest is malformed")
    if campaign["disposition"] != "frozen":
        raise QualificationRefusal("campaign disposition is not owner-frozen")
    return QualificationReport(5, 5, 2, 2, 48, 48, 15, 15, 8, 8, 8, 8, 1, 1, True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_root", type=pathlib.Path)
    parser.add_argument("--primary-id", required=True)
    parser.add_argument("--primary-manifest-sha256", required=True)
    parser.add_argument("--comparator-id", required=True)
    parser.add_argument("--comparator-manifest-sha256", required=True)
    arguments = parser.parse_args()
    try:
        report = verify_evidence_root(
            arguments.evidence_root,
            primary_id=arguments.primary_id,
            primary_manifest_sha256=arguments.primary_manifest_sha256,
            comparator_id=arguments.comparator_id,
            comparator_manifest_sha256=arguments.comparator_manifest_sha256,
        )
    except QualificationRefusal as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "verified", **asdict(report)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
