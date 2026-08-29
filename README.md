# Kilix Image Shop

Kilix Image Shop is the local-first image editor planned for Plebian OS 0.2.1.
The repository contains its immutable document-domain slice and the first
engine-integration slice. The domain provides typed content identities, checked
geometry and decode budgets, closed layer/mask/provenance values, the
`kilix.imageshop.project/v1` schema, canonical serialization, and
revision-checked pure command reduction. The engine boundary provides closed
engine-neutral graph, buffer, tile, cancellation, and H0 format contracts plus
a deterministic conformance fake. Native GI initialization, rendering,
persistence, history, presentation, and provider adapters enter as separate
reviewed slices.

## Product boundary

The editor unit owns the non-destructive document and layer model, adjustments,
selection and masks, transforms, paint and text, bounded history, project
persistence, colour management, tiled composition, import/export, CLI, TUI, and
the contained graphical surface. It must run on H0 without a model, GPU, or
network.

Model-backed operations form a later integration unit. No model runtime,
background-removal adapter, generation adapter, or GUI toolkit is included in
the current core.

The selected native image engine is the Plebian OS lazy group
`plebian.f115.image-engine`, containing the OD-7 GEGL/babl closure. It is an
operating-system dependency, not a Python runtime dependency.

## Development

Requirements:

- Debian Python 3.13.5 at `/usr/bin/python3`;
- the release-pinned uv 0.12.5 executable; and
- an offline uv cache containing the locked build closure.

Point `UV` at the pinned executable, then run:

~~~sh
make setup
make check
~~~

`setup` creates a system-site virtual environment so the later Debian GI
bindings remain visible, verifies that boundary, and performs an offline frozen
sync. The Python runtime dependency population remains 0/0. Engine-neutral
modules import 0/1 GI modules; the later guarded OD-7 runtime adapter owns that
sole native import boundary.

## Repository status

This repository is local-only. It has no configured remote, and its planning
authority permits no push, tag, publication, or release-pin movement.
