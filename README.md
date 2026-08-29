# Kilix Image Shop

Kilix Image Shop is the local-first image editor planned for Plebian OS 0.2.1.
This repository currently contains only its identity, licence, locked build
boundary, and entry tests.

## Product boundary

The editor unit owns the non-destructive document and layer model, adjustments,
selection and masks, transforms, paint and text, bounded history, project
persistence, colour management, tiled composition, import/export, CLI, TUI, and
the contained graphical surface. It must run on H0 without a model, GPU, or
network.

Model-backed operations form a later integration unit. No model runtime,
contract carrier, background-removal adapter, generation adapter, or GUI toolkit
is included in this initial skeleton.

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
sync. The initial Python runtime dependency population is 0/0.

## Repository status

This repository is local-only. It has no configured remote, and its planning
authority permits no push, tag, publication, or release-pin movement.
