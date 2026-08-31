#!/usr/bin/python3
"""Fail closed unless the process is inside the frozen H0 compute envelope."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import platform
import stat
import sys


EXPECTED_CPU_MODEL = "QEMU Virtual CPU version 2.5+"
EXPECTED_CPUS = 2
EXPECTED_DISK_BYTES = 40 * 1024 * 1024 * 1024
MIN_USABLE_MEMORY_KIB = 3_900_000
MAX_CONFIGURED_MEMORY_KIB = 4 * 1024 * 1024
CHECK_TOTAL = 8


class CapacityRefusal(RuntimeError):
    """The host could not be observed without weakening the capacity check."""


@dataclasses.dataclass(frozen=True, slots=True)
class CapacityObservation:
    debian_version: str
    architecture: str
    dmi_vendor: str
    dmi_product: str
    cpu_models: tuple[str, ...]
    effective_cpu_count: int
    memory_total_kib: int
    disk_bytes: int
    tmpdir_absolute: bool
    tmpdir_directory: bool
    tmpdir_symlink: bool
    tmpdir_mode: int
    tmpdir_uid: int
    effective_uid: int


@dataclasses.dataclass(frozen=True, slots=True)
class CapacityCheck:
    name: str
    passed: bool
    observed: str
    expected: str


def _read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CapacityRefusal(f"cannot read {path}: {exc}") from exc


def parse_memory_total_kib(payload: str) -> int:
    rows = [line.split() for line in payload.splitlines() if line.startswith("MemTotal:")]
    if len(rows) != 1 or len(rows[0]) != 3 or rows[0][2] != "kB":
        raise CapacityRefusal("MemTotal must be exactly one integer-kB row")
    try:
        value = int(rows[0][1], 10)
    except ValueError as exc:
        raise CapacityRefusal("MemTotal is not an integer") from exc
    if value <= 0:
        raise CapacityRefusal("MemTotal must be positive")
    return value


def parse_cpu_models(payload: str) -> tuple[str, ...]:
    models = tuple(
        line.split(":", 1)[1].strip()
        for line in payload.splitlines()
        if line.startswith("model name") and ":" in line
    )
    if not models or any(not model for model in models):
        raise CapacityRefusal("CPU model rows are absent or empty")
    return models


def observe_capacity() -> CapacityObservation:
    tmpdir_value = os.environ.get("TMPDIR")
    if not tmpdir_value:
        raise CapacityRefusal("TMPDIR is not set")
    tmpdir = pathlib.Path(tmpdir_value)
    try:
        tmpdir_stat = tmpdir.lstat()
    except OSError as exc:
        raise CapacityRefusal(f"TMPDIR cannot be inspected: {exc}") from exc

    try:
        effective_cpu_count = len(os.sched_getaffinity(0))
    except AttributeError:
        effective_cpu_count = os.cpu_count() or 0

    disk_sector_text = _read_text(pathlib.Path("/sys/class/block/vda/size")).strip()
    try:
        disk_sectors = int(disk_sector_text, 10)
    except ValueError as exc:
        raise CapacityRefusal("vda sector count is not an integer") from exc
    if disk_sectors <= 0:
        raise CapacityRefusal("vda sector count must be positive")

    return CapacityObservation(
        debian_version=_read_text(pathlib.Path("/etc/debian_version")).strip(),
        architecture=platform.machine(),
        dmi_vendor=_read_text(pathlib.Path("/sys/class/dmi/id/sys_vendor")).strip(),
        dmi_product=_read_text(pathlib.Path("/sys/class/dmi/id/product_name")).strip(),
        cpu_models=parse_cpu_models(_read_text(pathlib.Path("/proc/cpuinfo"))),
        effective_cpu_count=effective_cpu_count,
        memory_total_kib=parse_memory_total_kib(
            _read_text(pathlib.Path("/proc/meminfo"))
        ),
        disk_bytes=disk_sectors * 512,
        tmpdir_absolute=tmpdir.is_absolute(),
        tmpdir_directory=stat.S_ISDIR(tmpdir_stat.st_mode),
        tmpdir_symlink=stat.S_ISLNK(tmpdir_stat.st_mode),
        tmpdir_mode=stat.S_IMODE(tmpdir_stat.st_mode),
        tmpdir_uid=tmpdir_stat.st_uid,
        effective_uid=os.geteuid(),
    )


def evaluate_capacity(observation: CapacityObservation) -> tuple[CapacityCheck, ...]:
    cpu_models = set(observation.cpu_models)
    checks = (
        CapacityCheck(
            "os",
            observation.debian_version.split(".", 1)[0] == "13",
            observation.debian_version,
            "Debian major 13",
        ),
        CapacityCheck(
            "architecture",
            observation.architecture == "x86_64",
            observation.architecture,
            "x86_64",
        ),
        CapacityCheck(
            "machine",
            observation.dmi_vendor == "QEMU" and "Q35" in observation.dmi_product,
            f"vendor={observation.dmi_vendor},product={observation.dmi_product}",
            "vendor=QEMU,product contains Q35",
        ),
        CapacityCheck(
            "cpu_model",
            cpu_models == {EXPECTED_CPU_MODEL},
            ",".join(sorted(cpu_models)),
            EXPECTED_CPU_MODEL,
        ),
        CapacityCheck(
            "cpu_topology",
            observation.effective_cpu_count == EXPECTED_CPUS
            and len(observation.cpu_models) == EXPECTED_CPUS,
            f"effective={observation.effective_cpu_count},rows={len(observation.cpu_models)}",
            "effective=2,rows=2",
        ),
        CapacityCheck(
            "memory",
            MIN_USABLE_MEMORY_KIB
            <= observation.memory_total_kib
            <= MAX_CONFIGURED_MEMORY_KIB,
            str(observation.memory_total_kib),
            f"{MIN_USABLE_MEMORY_KIB}..{MAX_CONFIGURED_MEMORY_KIB} KiB",
        ),
        CapacityCheck(
            "disk",
            observation.disk_bytes == EXPECTED_DISK_BYTES,
            str(observation.disk_bytes),
            str(EXPECTED_DISK_BYTES),
        ),
        CapacityCheck(
            "tmpdir",
            observation.tmpdir_absolute
            and observation.tmpdir_directory
            and not observation.tmpdir_symlink
            and observation.tmpdir_mode == 0o700
            and observation.tmpdir_uid == observation.effective_uid,
            (
                f"absolute={observation.tmpdir_absolute},"
                f"directory={observation.tmpdir_directory},"
                f"symlink={observation.tmpdir_symlink},"
                f"mode={observation.tmpdir_mode:#o},"
                f"owner_match={observation.tmpdir_uid == observation.effective_uid}"
            ),
            "absolute directory, non-symlink, mode=0o700, owner_match=True",
        ),
    )
    if len(checks) != CHECK_TOTAL:
        raise AssertionError("capacity check population drifted")
    return checks


def main() -> int:
    try:
        checks = evaluate_capacity(observe_capacity())
    except CapacityRefusal as exc:
        print(f"F115_H0_CORE_ENVELOPE=0/{CHECK_TOTAL}", file=sys.stderr)
        print("F115_H0_INSTALLED_QUALIFICATION_CREDIT=0/1", file=sys.stderr)
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    for check in checks:
        result = int(check.passed)
        print(
            f"F115_H0_CORE_{check.name.upper()}={result}/1 "
            f"observed={check.observed} expected={check.expected}"
        )
    passed = sum(check.passed for check in checks)
    print(f"F115_H0_CORE_ENVELOPE={passed}/{CHECK_TOTAL}")
    print("F115_H0_INSTALLED_QUALIFICATION_CREDIT=0/1")
    return 0 if passed == CHECK_TOTAL else 1


if __name__ == "__main__":
    raise SystemExit(main())
