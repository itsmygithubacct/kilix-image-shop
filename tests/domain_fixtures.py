"""Synthetic, non-release domain fixtures used by the local test suite."""

from __future__ import annotations

from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.color import (
    AlphaAssociation,
    ColourSpace,
    ColourState,
    ConversionPolicy,
    EngineCompatibility,
)
from kilix_image_shop.domain.document import DocumentState, PROJECT_SCHEMA
from kilix_image_shop.domain.geometry import AffineTransform, Canvas, Rect
from kilix_image_shop.domain.identifiers import DocumentId, LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import (
    Adjustment,
    AdjustmentId,
    AdjustmentLayer,
    BlendMode,
    FontAxis,
    FontFallback,
    GroupLayer,
    MaskObject,
    MaskSource,
    OperationProvenance,
    Parameter,
    PixelLayer,
    Selection,
    SelectionKind,
    TextAlignment,
    TextLayer,
    TextLayout,
)


def object_id(character: str) -> ObjectId:
    return ObjectId(character * 64)


def layer_id(number: int) -> LayerId:
    return LayerId(f"00000000-0000-4000-8000-{number:012d}")


def compatibility() -> EngineCompatibility:
    return EngineCompatibility(
        schema=EngineCompatibility.SCHEMA,
        package_group_id="plebian.f115.image-engine",
        package_group_digest=object_id("2"),
        gegl_version="1:0.4.62-2+deb13u2",
        babl_version="1:0.1.114-2",
        python_gi_version="3.50.0-4+b1",
        gi_file_digest=object_id("3"),
        operation_count=203,
        operation_set_digest=object_id("4"),
        plugin_tree_digest=object_id("5"),
        working_format="RGBA u16",
        alpha_association=AlphaAssociation.STRAIGHT,
        mask_format="Y u8",
        mask_semantics="foreground-alpha",
        working_profile=object_id("1"),
        conversion_policy=ConversionPolicy.RELATIVE_COLORIMETRIC,
        resampling_kernel="nohalo",
        edge_mode="clamp",
        tile_halos=(("default", 0),),
        use_opencl=False,
        tile_cache_bytes=268435456,
        swap_compression="fast",
        threads=1,
        deterministic_preset="f115-synthetic-h0-u16-v1",
        babl_tolerance="0.0",
    )


def colour() -> ColourState:
    return ColourState(
        working_profile=object_id("1"),
        declared_space=ColourSpace.SRGB,
        conversion_policy=ConversionPolicy.RELATIVE_COLORIMETRIC,
    )


def empty_document() -> DocumentState:
    return DocumentState(
        schema=PROJECT_SCHEMA,
        document_id=DocumentId("00000000-0000-4000-8000-000000000115"),
        revision_id=RevisionId("00000000-0000-4000-8000-000000000001"),
        canvas=Canvas(64, 48),
        colour=colour(),
        engine_compatibility=compatibility(),
        assets=(),
        root_layer_ids=(),
        layers=(),
    )


def sample_assets() -> tuple[AssetRef, AssetRef]:
    return (
        AssetRef(
            digest=object_id("6"),
            byte_count=128,
            media_type=MediaType.PNG,
            width=64,
            height=48,
            profile_digest=object_id("1"),
            import_policy=ImportPolicy.COPIED,
        ),
        AssetRef(
            digest=object_id("7"),
            byte_count=96,
            media_type=MediaType.PNG,
            width=32,
            height=16,
            profile_digest=object_id("1"),
            import_policy=ImportPolicy.COPIED,
        ),
    )


def provenance() -> OperationProvenance:
    return OperationProvenance(
        schema=OperationProvenance.SCHEMA,
        operation="kilix.generate",
        provider="kilix.fake-provider",
        model_digest=None,
        runtime_digest=object_id("8"),
        prompt=None,
        seed=None,
        parameters=(Parameter("fixture-only", True),),
        source_layer_digest=None,
        occurred_at="2026-08-29T00:00:00+00:00",
    )


def sample_document() -> DocumentState:
    operation = provenance()
    mask = MaskObject(
        object_id=object_id("9"),
        width=64,
        height=48,
        origin_x=0,
        origin_y=0,
        source=MaskSource.HAND_PAINTED,
    )
    pixel = PixelLayer(
        layer_id=layer_id(1),
        name="Pixels",
        asset_digest=object_id("6"),
        mask=mask,
        operation_provenance=operation,
    )
    adjustment = AdjustmentLayer(
        layer_id=layer_id(2),
        name="Exposure",
        adjustment=Adjustment(
            AdjustmentId.EXPOSURE,
            (Parameter("stops", 0.5),),
        ),
        opacity_u16=49152,
        blend_mode=BlendMode.NORMAL,
    )
    text = TextLayer(
        layer_id=layer_id(3),
        name="Caption",
        text="Synthetic fixture",
        layout=TextLayout(32, 16, TextAlignment.START, "en"),
        font_digest=object_id("a"),
        face_index=0,
        axes=(FontAxis("wght", 400.0),),
        fallbacks=(FontFallback("Fixture Sans", "Fixture Sans", None, "exact"),),
        preview_asset_digest=object_id("7"),
        transform=AffineTransform(e=4.0, f=8.0),
    )
    group = GroupLayer(
        layer_id=layer_id(4),
        name="Root group",
        child_layer_ids=(pixel.layer_id, adjustment.layer_id, text.layer_id),
    )
    return DocumentState(
        schema=PROJECT_SCHEMA,
        document_id=DocumentId("00000000-0000-4000-8000-000000000115"),
        revision_id=RevisionId("00000000-0000-4000-8000-000000000002"),
        canvas=Canvas(64, 48),
        colour=colour(),
        engine_compatibility=compatibility(),
        assets=sample_assets(),
        root_layer_ids=(group.layer_id,),
        layers=(group, text, adjustment, pixel),
        selection=Selection(SelectionKind.RASTER, object_id("b"), Rect(1, 2, 8, 9)),
        provenance=(operation,),
    )
