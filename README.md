# Kilix Image Shop

Kilix Image Shop is the local-first, non-destructive image-editor core planned
for Plebian OS 0.2.1. This checkout materializes the frozen 45/45 functional
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
  diagnostics, and fake-provider conformance with 0/2 production providers; and
- 5/5 command-surface modules for finite configuration, readiness probes,
  deterministic rendering, the command verbs, and argument dispatch.

The Python runtime dependency population is 0/0. The native engine and codec
closure is an operating-system lazy group, not a Python dependency. The core
imports GI in 0/44 engine-neutral functional modules; the guarded runtime owns
the sole native boundary.

## Command surface

The wheel installs 1/1 console script, `kilix-image-shop`. Every verb consumes
the same core values the later toolkit surfaces will consume, and none of them
starts the engine, renders a pixel, or mutates a document:

| Verb | Result |
| --- | --- |
| `version` | product, schema and accepted OD-7 group identity |
| `doctor` | per-component readiness for the OD-7 group, interpreter and providers |
| `project info ROOT` | opens a project through all 10/10 validation classes |
| `project verify ROOT` | re-digests every object the current generation needs |
| `project generations ROOT` | lists retained generations and marks HEAD |
| `project recover ROOT GENERATION [--apply]` | previews, and only with `--apply` selects, another generation |
| `project gc ROOT [--apply]` | previews, and only with `--apply` quarantines, unreachable objects |
| `ops providers` | the production registry exactly as I1 ships it: 0/2 adapters |
| `ops diagnostics` | the closed 8/8 local diagnostic catalogue |
| `export preset ROOT FORMAT [--out PATH]` | binds one deterministic preset without rendering |
| `export verify SIDECAR PRESET [--artifact PATH]` | joins a sidecar to its preset and optional bytes |

`--output json` renders one canonical JSON object instead of aligned text, so
the surface is scriptable without parsing prose. Exit statuses come from a
closed set: `0` success, `1` internal error, `2` usage, `3` a required
component is unavailable, `4` invalid project or carrier data. Every project
ceiling is finite and printed; `--max-*` overrides are explicit and an open
ceiling is refused rather than defaulted.

`doctor` exits `3` on any host that lacks the complete OD-7 package group. That
is a readiness probe, not an installed-H0 result: no local invocation grants
qualification credit.

## Admission boundary

Local core materialization covers slices 1/8 through 7/8. Slice 8/8, the Kilix
tab surface and toolkit adapters, is BLOCKED at 0/1 toolkit selections, owned
by the release owner. The contained GUI, the `kilix-tui-utils` shell surface,
the Kilix tab dispatch and the `kilix-content` catalog entry are 0/4 delivered
here; each belongs to a repository this stream does not own. Editing verbs are
withheld at 0/1 pending the profile-object source recorded in
[`QUALIFICATION.md`](QUALIFICATION.md).

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

The working copy configures 0/0 remotes; publication uses an explicit URL at
push time. Authorized publication is limited to `work/*` and `archive/*` refs
on the private remote `itsmygithubacct/kilix-image-shop`. Pushes to `main`,
release tags, force-pushes, history rewrites, visibility changes, package or
release-artifact publication and release-pin movement remain refused and
reserved to the owner. See [`PUBLICATION.md`](PUBLICATION.md).
