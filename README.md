# Kilix Image Shop

Kilix Image Shop is the local-first image editor planned for Plebian OS 0.2.1.
The repository contains its immutable document-domain slice and all seven
engine-integration slices. The domain provides typed content identities, checked
geometry and decode budgets, closed layer/mask/provenance values, the
`kilix.imageshop.project/v1` schema, canonical serialization, and
revision-checked pure command reduction. The engine boundary provides closed
engine-neutral graph, buffer, tile, cancellation, and H0 format contracts plus
a deterministic conformance fake. Its guarded runtime validates accepted
package/plugin carriers, deterministic environment and private swap state,
applies and reads back the complete H0 configuration, verifies native identity,
and publishes only after a 1x1 smoke graph. The third slice imports exact H0
pixel buffers, materializes digest-bound ICC carriers into private session
storage, and compiles the closed nine-family graph through a frozen operation
registry without accepting native operation or property strings from project
data. The fourth slice adds the three-level proxy pyramid, complete-only
manifests, nearest-not-coarser zoom selection, dependency-aware invalidation,
and bounded proxy reads with reusable native level graphs. The fifth slice adds
integer-only tile partitioning, the four-class priority queue, revision and
cancellation publication gates, and one completing `blit_buffer` call for each
bounded native tile. The sixth slice adds immutable copy-on-write foreground
masks with canonical 256×256 sparse-tile identities and an all-or-nothing
full-resolution tile worker. The seventh slice publishes nine safe diagnostic
groups and adds a fail-closed binder for the frozen fixture, package, harness,
and campaign carriers. Atomic encoded-file replacement remains owned by the
later export-pipeline slice; persistence, history, presentation, and provider
adapters likewise enter as separate reviewed slices.

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
modules import 0/1 GI modules, and package import eagerly loads 0/1 GI modules;
the guarded OD-7 runtime owns the sole native import boundary.

The qualification-carrier verifier is documented in `QUALIFICATION.md`. Its
unit fixtures demonstrate structure and refusal behavior only; they are not H0
release evidence.

## Repository status

This repository is local-only. It has no configured remote, and its planning
authority permits no push, tag, publication, or release-pin movement.
