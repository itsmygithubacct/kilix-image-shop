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
from .compatibility import RuntimeConfiguration, require_compatible
from .formats import RenderTier, TierFormatPolicy
from .runtime import ImageRuntime, RuntimeHandle

__all__ = (
    "BufferRef",
    "CancelToken",
    "EngineCapabilities",
    "EngineFailure",
    "FakeImageEngine",
    "GraphNodeKind",
    "GraphNodeSpec",
    "GraphSpec",
    "ImageEngine",
    "ImageRuntime",
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
