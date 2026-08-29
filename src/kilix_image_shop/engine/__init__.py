"""Engine-neutral contracts for deterministic image processing."""

from .api import (
    BufferRef,
    CancelToken,
    EngineCapabilities,
    EngineFailure,
    FakeImageEngine,
    GraphNodeKind,
    GraphNodeSpec,
    GraphSpec,
    ImageEngine,
    PixelFormat,
    PixelSpec,
    TileRequest,
    TileResult,
)
from .compatibility import OperationRegistry, RuntimeConfiguration, require_compatible
from .formats import RenderTier, TierFormatPolicy
from .runtime import CompiledGraphPlan, ImageRuntime, Od7ImageEngine, RuntimeHandle

__all__ = (
    "BufferRef",
    "CancelToken",
    "CompiledGraphPlan",
    "EngineCapabilities",
    "EngineFailure",
    "FakeImageEngine",
    "GraphNodeKind",
    "GraphNodeSpec",
    "GraphSpec",
    "ImageEngine",
    "ImageRuntime",
    "Od7ImageEngine",
    "OperationRegistry",
    "PixelFormat",
    "PixelSpec",
    "RenderTier",
    "RuntimeConfiguration",
    "RuntimeHandle",
    "TierFormatPolicy",
    "TileRequest",
    "TileResult",
    "require_compatible",
)
