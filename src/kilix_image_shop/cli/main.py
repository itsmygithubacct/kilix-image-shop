"""Argument parsing and dispatch for the toolkit-free command-line surface."""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Callable, Sequence, TextIO

from kilix_image_shop.store.layout import ProjectLimits, StoreError

from . import commands
from .configuration import PROGRAM_NAME, ExitCode, project_limits
from .presentation import OutputFormat, PresentationError, render


_LIMIT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("--max-manifest-bytes", "max_manifest_bytes"),
    ("--max-objects", "max_objects"),
    ("--max-object-bytes", "max_object_bytes"),
    ("--max-total-object-bytes", "max_total_object_bytes"),
    ("--max-layers", "max_layers"),
    ("--max-group-depth", "max_group_depth"),
)


def _limit_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("project ceilings")
    for flag, destination in _LIMIT_OPTIONS:
        group.add_argument(
            flag,
            dest=destination,
            type=int,
            default=None,
            help="override one finite project ceiling",
        )
    return parent


def build_parser() -> argparse.ArgumentParser:
    """Build the complete command tree; every verb is explicit and non-interactive."""

    limits = _limit_parent()
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Local-first non-destructive image editor: command surface.",
    )
    parser.add_argument(
        "--output",
        choices=tuple(item.value for item in OutputFormat),
        default=OutputFormat.TEXT.value,
        help="render human-aligned text or one canonical JSON object",
    )
    groups = parser.add_subparsers(dest="group")

    groups.add_parser("version", help="print product, schema and engine identity")
    groups.add_parser("doctor", help="probe installed components without starting the engine")

    project = groups.add_parser("project", help="inspect and maintain one project")
    project_verbs = project.add_subparsers(dest="verb")

    info = project_verbs.add_parser("info", parents=[limits], help="open and describe a project")
    info.add_argument("root", help="project root directory")

    verify = project_verbs.add_parser(
        "verify",
        parents=[limits],
        help="re-digest every object the current generation depends on",
    )
    verify.add_argument("root", help="project root directory")

    generations = project_verbs.add_parser(
        "generations",
        parents=[limits],
        help="list retained generations and mark HEAD",
    )
    generations.add_argument("root", help="project root directory")

    recover = project_verbs.add_parser(
        "recover",
        parents=[limits],
        help="preview, and only with --apply select, another generation",
    )
    recover.add_argument("root", help="project root directory")
    recover.add_argument("generation", help="generation content identity")
    recover.add_argument(
        "--apply",
        action="store_true",
        help="replace HEAD after retaining the current HEAD bytes",
    )

    collect = project_verbs.add_parser(
        "gc",
        parents=[limits],
        help="preview, and only with --apply quarantine, unreachable objects",
    )
    collect.add_argument("root", help="project root directory")
    collect.add_argument(
        "--apply",
        action="store_true",
        help="move unreachable objects into a private quarantine",
    )

    ops = groups.add_parser("ops", help="report the operation substrate")
    ops_verbs = ops.add_subparsers(dest="verb")
    ops_verbs.add_parser("providers", help="report installed operation providers")
    ops_verbs.add_parser("diagnostics", help="print the closed local diagnostic catalogue")

    export = groups.add_parser("export", help="bind and verify deterministic exports")
    export_verbs = export.add_subparsers(dest="verb")

    preset = export_verbs.add_parser(
        "preset",
        parents=[limits],
        help="bind the current generation to one deterministic preset",
    )
    preset.add_argument("root", help="project root directory")
    preset.add_argument("format", help="export format: png, webp, jpeg or tiff")
    preset.add_argument("--out", dest="out", default=None, help="write the canonical preset here")

    verify_export = export_verbs.add_parser(
        "verify",
        help="verify a sidecar against its preset and optional artifact bytes",
    )
    verify_export.add_argument("sidecar", help="export provenance sidecar")
    verify_export.add_argument("preset", help="export preset carrier")
    verify_export.add_argument(
        "--artifact",
        dest="artifact",
        default=None,
        help="also re-digest the exported artifact bytes",
    )

    return parser


def _limits_from(arguments: argparse.Namespace) -> ProjectLimits:
    values = {
        destination: getattr(arguments, destination, None)
        for _, destination in _LIMIT_OPTIONS
    }
    return project_limits(**values)


def _dispatch(arguments: argparse.Namespace) -> commands.Outcome:
    group = arguments.group
    if group == "version":
        return commands.version_command()
    if group == "doctor":
        return commands.doctor_command()
    if group == "project":
        limits = _limits_from(arguments)
        verb = arguments.verb
        if verb == "info":
            return commands.project_info_command(arguments.root, limits)
        if verb == "verify":
            return commands.project_verify_command(arguments.root, limits)
        if verb == "generations":
            return commands.project_generations_command(arguments.root, limits)
        if verb == "recover":
            return commands.project_recover_command(
                arguments.root,
                arguments.generation,
                apply=arguments.apply,
                limits=limits,
            )
        if verb == "gc":
            return commands.project_gc_command(
                arguments.root,
                apply=arguments.apply,
                limits=limits,
            )
    if group == "ops":
        if arguments.verb == "providers":
            return commands.ops_providers_command()
        if arguments.verb == "diagnostics":
            return commands.ops_diagnostics_command()
    if group == "export":
        if arguments.verb == "preset":
            return commands.export_preset_command(
                arguments.root,
                arguments.format,
                output_argument=arguments.out,
                limits=_limits_from(arguments),
            )
        if arguments.verb == "verify":
            return commands.export_verify_command(
                arguments.sidecar,
                arguments.preset,
                artifact_argument=arguments.artifact,
            )
    raise commands.CommandError("no command was selected", ExitCode.USAGE)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one command and return its exit status; never raise to the shell."""

    out = sys.stdout if stdout is None else stdout
    error = sys.stderr if stderr is None else stderr
    parser = build_parser()
    try:
        with contextlib.redirect_stderr(error):
            arguments = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exit_request:
        code = exit_request.code
        if code is None or code == 0:
            return int(ExitCode.OK)
        return int(ExitCode.USAGE)
    try:
        outcome = _dispatch(arguments)
    except commands.CommandError as failure:
        print(f"{PROGRAM_NAME}: {failure}", file=error)
        if arguments.group is None:
            with contextlib.redirect_stdout(error):
                parser.print_usage()
        return int(failure.exit_code)
    except StoreError as failure:
        print(f"{PROGRAM_NAME}: {failure}", file=error)
        return int(ExitCode.USAGE)
    except Exception as failure:  # noqa: BLE001 - the shell boundary never leaks a traceback
        print(
            f"{PROGRAM_NAME}: internal error: {type(failure).__name__}",
            file=error,
        )
        return int(ExitCode.INTERNAL)
    try:
        rendered = render(outcome.report, OutputFormat(arguments.output))
    except PresentationError as failure:
        print(f"{PROGRAM_NAME}: {failure}", file=error)
        return int(ExitCode.INTERNAL)
    if rendered:
        out.write(rendered)
    return int(outcome.exit_code)


__all__ = ("build_parser", "main")


if __name__ == "__main__":  # pragma: no cover - process entry only
    sys.exit(main())
