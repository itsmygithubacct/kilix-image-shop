"""Argument parsing and dispatch for the toolkit-free command-line surface."""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Sequence, TextIO

from kilix_image_shop.domain.assets import MediaType
from kilix_image_shop.domain.color import ColourSpace
from kilix_image_shop.domain.layers import AdjustmentId, BlendMode
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

    create = project_verbs.add_parser(
        "create",
        parents=[limits],
        help="create an empty atomic project from a compatibility carrier",
    )
    create.add_argument("root", help="new project root directory")
    create.add_argument("compatibility", help="canonical engine compatibility JSON")
    create.add_argument("--width", type=int, required=True, help="canvas width")
    create.add_argument("--height", type=int, required=True, help="canvas height")
    create.add_argument("--document-id", default=None, help="canonical UUID; generated if absent")
    create.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")
    create.add_argument(
        "--declared-space",
        choices=tuple(item.value for item in ColourSpace),
        default=ColourSpace.SRGB.value,
        help="declared document colour space",
    )

    info = project_verbs.add_parser("info", parents=[limits], help="open and describe a project")
    info.add_argument("root", help="project root directory")

    layers = project_verbs.add_parser(
        "layers",
        parents=[limits],
        help="list the immutable layer tree selected by HEAD",
    )
    layers.add_argument("root", help="project root directory")

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

    edit = groups.add_parser("edit", help="commit validated document mutations")
    edit_verbs = edit.add_subparsers(dest="verb")

    import_asset = edit_verbs.add_parser(
        "import",
        parents=[limits],
        help="copy one encoded image carrier into a pixel layer",
    )
    import_asset.add_argument("root", help="project root directory")
    import_asset.add_argument("asset", help="encoded PNG, JPEG, WebP or TIFF carrier")
    import_asset.add_argument(
        "--media-type",
        choices=tuple(item.value for item in MediaType),
        required=True,
        help="declared closed media type",
    )
    import_asset.add_argument("--width", type=int, required=True, help="decoded width")
    import_asset.add_argument("--height", type=int, required=True, help="decoded height")
    import_asset.add_argument(
        "--profile-sha256",
        required=True,
        help="embedded or assigned ICC profile content identity",
    )
    import_asset.add_argument("--name", default="Imported pixels", help="layer name")
    import_asset.add_argument("--layer-id", default=None, help="canonical UUID; generated if absent")
    import_asset.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")
    import_asset.add_argument("--parent-id", default=None, help="optional group-layer UUID")
    import_asset.add_argument("--index", type=int, default=0, help="zero-based insertion index")

    adjustment = edit_verbs.add_parser(
        "adjustment",
        parents=[limits],
        help="add one non-destructive adjustment layer",
    )
    adjustment.add_argument("root", help="project root directory")
    adjustment.add_argument(
        "adjustment",
        choices=tuple(item.value for item in AdjustmentId),
        help="closed adjustment identity",
    )
    adjustment.add_argument(
        "--parameter",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="repeat for every required adjustment parameter",
    )
    adjustment.add_argument("--name", default="Adjustment", help="layer name")
    adjustment.add_argument("--layer-id", default=None, help="canonical UUID; generated if absent")
    adjustment.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")
    adjustment.add_argument("--parent-id", default=None, help="optional group-layer UUID")
    adjustment.add_argument("--index", type=int, default=0, help="zero-based insertion index")

    mask = edit_verbs.add_parser(
        "mask",
        parents=[limits],
        help="attach or replace a full-canvas editable Y u8 mask",
    )
    mask.add_argument("root", help="project root directory")
    mask.add_argument("layer", help="target layer UUID")
    mask.add_argument("mask", help="headerless full-canvas Y u8 bytes")
    mask.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")

    layer = edit_verbs.add_parser(
        "layer",
        parents=[limits],
        help="change common properties on one layer",
    )
    layer.add_argument("root", help="project root directory")
    layer.add_argument("layer", help="target layer UUID")
    layer.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")
    layer.add_argument("--name", default=None, help="replacement layer name")
    visibility = layer.add_mutually_exclusive_group()
    visibility.add_argument("--visible", dest="visible", action="store_true")
    visibility.add_argument("--hidden", dest="visible", action="store_false")
    layer.set_defaults(visible=None)
    layer.add_argument("--opacity-u16", type=int, default=None, help="opacity in [0, 65535]")
    layer.add_argument(
        "--blend-mode",
        choices=tuple(item.value for item in BlendMode),
        default=None,
        help="closed blend mode",
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
        if verb == "create":
            return commands.project_create_command(
                arguments.root,
                arguments.compatibility,
                width=arguments.width,
                height=arguments.height,
                document_id_argument=arguments.document_id,
                revision_id_argument=arguments.revision_id,
                declared_space_argument=arguments.declared_space,
                limits=limits,
            )
        if verb == "info":
            return commands.project_info_command(arguments.root, limits)
        if verb == "layers":
            return commands.project_layers_command(arguments.root, limits)
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
    if group == "edit":
        limits = _limits_from(arguments)
        verb = arguments.verb
        if verb == "import":
            return commands.edit_import_command(
                arguments.root,
                arguments.asset,
                media_type_argument=arguments.media_type,
                width=arguments.width,
                height=arguments.height,
                profile_argument=arguments.profile_sha256,
                name=arguments.name,
                layer_id_argument=arguments.layer_id,
                revision_id_argument=arguments.revision_id,
                parent_id_argument=arguments.parent_id,
                index=arguments.index,
                limits=limits,
            )
        if verb == "adjustment":
            return commands.edit_adjustment_command(
                arguments.root,
                arguments.adjustment,
                parameter_arguments=tuple(arguments.parameter),
                name=arguments.name,
                layer_id_argument=arguments.layer_id,
                revision_id_argument=arguments.revision_id,
                parent_id_argument=arguments.parent_id,
                index=arguments.index,
                limits=limits,
            )
        if verb == "mask":
            return commands.edit_mask_command(
                arguments.root,
                arguments.layer,
                arguments.mask,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "layer":
            return commands.edit_layer_command(
                arguments.root,
                arguments.layer,
                revision_id_argument=arguments.revision_id,
                name=arguments.name,
                visible=arguments.visible,
                opacity_u16=arguments.opacity_u16,
                blend_mode_argument=arguments.blend_mode,
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
