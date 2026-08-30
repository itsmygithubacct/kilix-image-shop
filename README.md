# Kilix Image Shop

Kilix Image Shop is the local-first, non-destructive image-editor core planned
for Plebian OS 0.2.1. This checkout materializes the frozen 40/40 functional
module population without a model, GPU, network dependency, provider adapter,
or GUI toolkit.

The current core contains:

- 2/2 application-boundary modules for command transactions and adapter ports;
- 7/7 immutable document-domain modules with canonical project-v1 persistence;
- 9/9 engine/render modules covering the closed OD-7 GEGL/babl boundary,
  proxies, bounded tiles, invalidation, render planning, and composition;
- 6/6 crash-safe project-store modules for objects, generations, locking,
  recovery, and reachability garbage collection;
- 9/9 bounded-history and conventional-editing modules for adjustments,
  selection, masks, transforms, paint, and editable text;
- 3/3 deterministic export modules for PNG, JPEG, WebP, TIFF, metadata policy,
  bounded staging, atomic image replacement, and provenance sidecars; and
- 4/4 generic operation modules for typed progress, cancellation, fixed local
  diagnostics, and fake-provider conformance with 0/2 production providers.

The Python runtime dependency population is 0/0. The native engine and codec
closure is an operating-system lazy group, not a Python dependency. The core
imports GI in 0/39 engine-neutral functional modules; the guarded runtime owns
the sole native boundary.

## Admission boundary

Local core materialization covers slices 1/8 through 7/8. Slice 8/8, the Kilix
tab surface and toolkit adapters, is BLOCKED at 0/1 toolkit selections, owned
by the release owner. The CLI/TUI/shared-shell packaging integration is also
outside this repository-only change.

The two admitted model-backed operations have 0/2 production adapters in I1.
Their later I2A integrations remain separately candidate-bound; operation
output can enter the document only through the validated
`ApplyOperationOutput` command.

This repository therefore supplies an independently reviewable core, not an
independent claim of release admission. The review packet is
[`REVIEW-HANDOFF.md`](REVIEW-HANDOFF.md), and the external qualification gates
are recorded in [`QUALIFICATION.md`](QUALIFICATION.md).

## Development

Requirements:

- Debian Python 3.13.5 at `/usr/bin/python3`;
- the release-pinned uv 0.12.5 executable; and
- an offline uv cache containing the locked 6/6-package build closure.

Point `UV` at the pinned executable, then run:

~~~sh
make setup
make check
~~~

`setup` creates a system-site virtual environment so archive-owned Debian GI
bindings remain visible, verifies that boundary, and performs an offline frozen
sync. `check` executes 5/5 aggregate phases: lock verification, tests, wheel and
source builds, legal-carrier checks, and repository hygiene. The Makefile
exposes the frozen 7/7 bounded targets.

The qualification-carrier verifier in `tools/verify_f115_qualification.py`
checks the structure and content identity of an owner-supplied evidence packet.
Its synthetic unit fixtures receive 0/1 installed-H0 evidence credit.

## Repository status

This repository is local-only and has 0/0 configured remotes. No push, tag,
package publication, release artifact publication, or release-pin movement is
authorized.
