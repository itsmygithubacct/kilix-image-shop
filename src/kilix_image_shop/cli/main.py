"""Argument parsing and dispatch for the toolkit-free command-line surface."""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Sequence, TextIO

from kilix_image_shop.domain.assets import MediaType
from kilix_image_shop.domain.color import ColourSpace
from kilix_image_shop.domain.layers import AdjustmentId, BlendMode, TextAlignment
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


def _add_editable_text_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("font", help="project-owned font carrier")
    parser.add_argument("--text", required=True, help="complete editable text content")
    parser.add_argument(
        "--preview-asset-sha256",
        required=True,
        help="already-declared preview asset identity",
    )
    parser.add_argument("--width", type=int, required=True, help="text layout width")
    parser.add_argument("--height", type=int, required=True, help="text layout height")
    parser.add_argument(
        "--alignment",
        choices=tuple(item.value for item in TextAlignment),
        default=TextAlignment.START.value,
        help="closed text alignment",
    )
    parser.add_argument("--language", default="und", help="bounded language identity")
    parser.add_argument("--face-index", type=int, default=0, help="font face index")
    parser.add_argument(
        "--axis",
        action="append",
        default=[],
        metavar="TAG=NUMBER",
        help="repeat for each pinned variable-font axis",
    )


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

    mask_paint = edit_verbs.add_parser(
        "mask-paint",
        parents=[limits],
        help="commit a full-mask paint result with an exact sparse tile delta",
    )
    mask_paint.add_argument("root", help="project root directory")
    mask_paint.add_argument("layer", help="target layer UUID")
    mask_paint.add_argument("mask", help="headerless painted Y u8 bytes")
    mask_paint.add_argument(
        "--before-sha256",
        required=True,
        help="required current mask identity; stale results are refused",
    )
    mask_paint.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

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

    group = edit_verbs.add_parser(
        "group",
        parents=[limits],
        help="add one empty editable group",
    )
    group.add_argument("root", help="project root directory")
    group.add_argument("--name", default="Group", help="layer name")
    group.add_argument("--layer-id", default=None, help="canonical UUID; generated if absent")
    group.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")
    group.add_argument("--parent-id", default=None, help="optional group-layer UUID")
    group.add_argument("--index", type=int, default=0, help="zero-based insertion index")

    text = edit_verbs.add_parser(
        "text",
        parents=[limits],
        help="add editable text with a copied pinned font",
    )
    text.add_argument("root", help="project root directory")
    _add_editable_text_arguments(text)
    text.add_argument("--name", default="Text", help="layer name")
    text.add_argument("--layer-id", default=None, help="canonical UUID; generated if absent")
    text.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")
    text.add_argument("--parent-id", default=None, help="optional group-layer UUID")
    text.add_argument("--index", type=int, default=0, help="zero-based insertion index")

    text_set = edit_verbs.add_parser(
        "text-set",
        parents=[limits],
        help="replace editable text and its primary font/layout identity",
    )
    text_set.add_argument("root", help="project root directory")
    text_set.add_argument("layer", help="target text-layer UUID")
    _add_editable_text_arguments(text_set)
    text_set.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    flatten_result = edit_verbs.add_parser(
        "flatten-result",
        parents=[limits],
        help="commit an already-rendered local flatten result",
    )
    flatten_result.add_argument("root", help="project root directory")
    flatten_result.add_argument("carrier", help="encoded flatten output carrier")
    flatten_result.add_argument(
        "--source-layer",
        action="append",
        default=[],
        required=True,
        help="repeat in sibling flatten order",
    )
    flatten_result.add_argument(
        "--media-type",
        choices=tuple(item.value for item in MediaType),
        required=True,
        help="declared closed media type",
    )
    flatten_result.add_argument("--width", type=int, required=True, help="decoded width")
    flatten_result.add_argument("--height", type=int, required=True, help="decoded height")
    flatten_result.add_argument("--profile-sha256", required=True, help="profile identity")
    flatten_result.add_argument("--name", default="Flattened", help="output layer name")
    flatten_result.add_argument(
        "--layer-id", default=None, help="canonical output UUID; generated if absent"
    )
    flatten_result.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    adjustment_set = edit_verbs.add_parser(
        "adjustment-set",
        parents=[limits],
        help="replace an existing adjustment layer's parameters",
    )
    adjustment_set.add_argument("root", help="project root directory")
    adjustment_set.add_argument("layer", help="target adjustment-layer UUID")
    adjustment_set.add_argument(
        "adjustment",
        choices=tuple(item.value for item in AdjustmentId),
        help="closed adjustment identity",
    )
    adjustment_set.add_argument(
        "--parameter",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="repeat for every required adjustment parameter",
    )
    adjustment_set.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    mask_remove = edit_verbs.add_parser(
        "mask-remove",
        parents=[limits],
        help="remove one existing editable layer mask",
    )
    mask_remove.add_argument("root", help="project root directory")
    mask_remove.add_argument("layer", help="target layer UUID")
    mask_remove.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    layer_remove = edit_verbs.add_parser(
        "layer-remove",
        parents=[limits],
        help="remove one layer; non-empty groups require --recursive",
    )
    layer_remove.add_argument("root", help="project root directory")
    layer_remove.add_argument("layer", help="target layer UUID")
    layer_remove.add_argument("--recursive", action="store_true", help="also remove descendants")
    layer_remove.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    layer_move = edit_verbs.add_parser(
        "layer-move",
        parents=[limits],
        help="move one layer to an exact root/group position",
    )
    layer_move.add_argument("root", help="project root directory")
    layer_move.add_argument("layer", help="target layer UUID")
    layer_move.add_argument("--parent-id", default=None, help="optional target group UUID")
    layer_move.add_argument("--index", type=int, required=True, help="zero-based insertion index")
    layer_move.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    transform = edit_verbs.add_parser(
        "transform",
        parents=[limits],
        help="replace one checked affine layer transform",
    )
    transform.add_argument("root", help="project root directory")
    transform.add_argument("layer", help="target pixel, text or group layer UUID")
    transform.add_argument("a", type=float)
    transform.add_argument("b", type=float)
    transform.add_argument("c", type=float)
    transform.add_argument("d", type=float)
    transform.add_argument("e", type=float)
    transform.add_argument("f", type=float)
    transform.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    crop = edit_verbs.add_parser(
        "crop",
        parents=[limits],
        help="replace checked canvas geometry without resampling layers",
    )
    crop.add_argument("root", help="project root directory")
    crop.add_argument("--origin-x", type=int, default=0)
    crop.add_argument("--origin-y", type=int, default=0)
    crop.add_argument("--width", type=int, required=True)
    crop.add_argument("--height", type=int, required=True)
    crop.add_argument("--revision-id", default=None, help="canonical UUID; generated if absent")

    selection = edit_verbs.add_parser(
        "selection",
        parents=[limits],
        help="set one bounded vector or raster selection object",
    )
    selection.add_argument("root", help="project root directory")
    selection.add_argument("selection", help="selection object carrier")
    selection.add_argument(
        "--kind", choices=("vector", "raster"), required=True, help="closed selection kind"
    )
    selection.add_argument("--x", type=int, required=True)
    selection.add_argument("--y", type=int, required=True)
    selection.add_argument("--width", type=int, required=True)
    selection.add_argument("--height", type=int, required=True)
    selection.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
    )

    selection_clear = edit_verbs.add_parser(
        "selection-clear",
        parents=[limits],
        help="clear one existing selection",
    )
    selection_clear.add_argument("root", help="project root directory")
    selection_clear.add_argument(
        "--revision-id", default=None, help="canonical UUID; generated if absent"
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
        if verb == "mask-paint":
            return commands.edit_mask_paint_command(
                arguments.root,
                arguments.layer,
                arguments.mask,
                before_argument=arguments.before_sha256,
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
        if verb == "group":
            return commands.edit_group_command(
                arguments.root,
                name=arguments.name,
                layer_id_argument=arguments.layer_id,
                revision_id_argument=arguments.revision_id,
                parent_id_argument=arguments.parent_id,
                index=arguments.index,
                limits=limits,
            )
        if verb == "text":
            return commands.edit_text_command(
                arguments.root,
                arguments.font,
                text=arguments.text,
                width=arguments.width,
                height=arguments.height,
                alignment_argument=arguments.alignment,
                language=arguments.language,
                face_index=arguments.face_index,
                axis_arguments=tuple(arguments.axis),
                preview_argument=arguments.preview_asset_sha256,
                name=arguments.name,
                layer_id_argument=arguments.layer_id,
                revision_id_argument=arguments.revision_id,
                parent_id_argument=arguments.parent_id,
                index=arguments.index,
                limits=limits,
            )
        if verb == "text-set":
            return commands.edit_text_set_command(
                arguments.root,
                arguments.layer,
                arguments.font,
                text=arguments.text,
                width=arguments.width,
                height=arguments.height,
                alignment_argument=arguments.alignment,
                language=arguments.language,
                face_index=arguments.face_index,
                axis_arguments=tuple(arguments.axis),
                preview_argument=arguments.preview_asset_sha256,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "flatten-result":
            return commands.edit_flatten_result_command(
                arguments.root,
                arguments.carrier,
                source_layer_arguments=tuple(arguments.source_layer),
                media_type_argument=arguments.media_type,
                width=arguments.width,
                height=arguments.height,
                profile_argument=arguments.profile_sha256,
                name=arguments.name,
                layer_id_argument=arguments.layer_id,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "adjustment-set":
            return commands.edit_adjustment_set_command(
                arguments.root,
                arguments.layer,
                arguments.adjustment,
                parameter_arguments=tuple(arguments.parameter),
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "mask-remove":
            return commands.edit_mask_remove_command(
                arguments.root,
                arguments.layer,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "layer-remove":
            return commands.edit_layer_remove_command(
                arguments.root,
                arguments.layer,
                recursive=arguments.recursive,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "layer-move":
            return commands.edit_layer_move_command(
                arguments.root,
                arguments.layer,
                parent_id_argument=arguments.parent_id,
                index=arguments.index,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "transform":
            return commands.edit_transform_command(
                arguments.root,
                arguments.layer,
                (
                    arguments.a,
                    arguments.b,
                    arguments.c,
                    arguments.d,
                    arguments.e,
                    arguments.f,
                ),
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "crop":
            return commands.edit_crop_command(
                arguments.root,
                origin_x=arguments.origin_x,
                origin_y=arguments.origin_y,
                width=arguments.width,
                height=arguments.height,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "selection":
            return commands.edit_selection_command(
                arguments.root,
                arguments.selection,
                kind_argument=arguments.kind,
                x=arguments.x,
                y=arguments.y,
                width=arguments.width,
                height=arguments.height,
                revision_id_argument=arguments.revision_id,
                limits=limits,
            )
        if verb == "selection-clear":
            return commands.edit_selection_clear_command(
                arguments.root,
                revision_id_argument=arguments.revision_id,
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
