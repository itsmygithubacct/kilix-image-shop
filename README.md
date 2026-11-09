# Kilix Image Shop

The toolkit-free core of a local-first, non-destructive image editor for Kilix.

**Status:** core only, not part of Plebian-OS. This README describes the
repository's default branch, `work/0.2.1-f115`. The Plebian-OS 0.2.2 release
notes list it among stream deliverables that do not ship in the image. The
Kilix for Windows preview's WSL app catalog (`windows/wsl/app-catalog.json` in
kilix) lists it as "Image Shop", pinned to `4c0d9d19` (this branch's head),
with project storage, document edits and export presets available and pixel
rendering, the graphical editor and model operations unavailable. It has no
model and 0/2 production model providers. The repository is now public on
GitHub; [PUBLICATION.md](PUBLICATION.md) and
[docs/development.md](docs/development.md) still describe the private remote it
was first published to.

## What it does

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

## Quick start

Requirements:

- Debian Python 3.13.5 at `/usr/bin/python3`;
- the release-pinned uv 0.12.5 executable; and
- an offline uv cache containing the locked 6/6-package build closure.

Point `UV` at the pinned executable, then run:

~~~sh
make setup
make check
~~~

Then, with the console script the setup installs into `.venv`:

```sh
.venv/bin/kilix-image-shop version
.venv/bin/kilix-image-shop doctor
.venv/bin/kilix-image-shop ops providers
```

`doctor` exits `3` until the native GEGL/babl package group is installed; the
project and edit verbs work without it.

## Usage

| Command | What it does |
|---|---|
| `kilix-image-shop version` / `doctor` | Product, schema and engine identity; per-component readiness. |
| `project create ROOT COMPATIBILITY --width W --height H` | Create an empty canonical project and verify its first generation. |
| `project info` / `layers` / `verify` / `generations ROOT` | Open, list, re-digest and list generations of a project. |
| `project recover ROOT GENERATION [--apply]` / `project gc ROOT [--apply]` | Preview, and only with `--apply` change, the selected generation or unreachable objects. |
| `edit import ROOT ASSET ...` | Copy an encoded PNG, JPEG, WebP or TIFF into a new pixel layer. |
| `edit adjustment`, `mask`, `layer`, `group`, `text`, `transform`, `crop`, `selection`, ... | Commit one checked document mutation as a new verified generation. |
| `ops providers` / `ops diagnostics` | The provider registry (0/2 adapters) and the local diagnostic catalogue. |
| `export preset ROOT FORMAT` / `export verify SIDECAR PRESET` | Bind a deterministic export preset; join a provenance sidecar to it. |
| `--output json` | One canonical JSON object instead of aligned text. |
| `make setup` / `make check` | Offline locked environment; lock, tests, builds, legal carriers and hygiene. |

The full verb table, exit statuses and ceilings are in
[docs/command-surface.md](docs/command-surface.md).

## How it works

```
kilix-image-shop verbs (later: toolkit surfaces, same commands)
        |
application: validate a command -> one new immutable DocumentState, or none
        |                                    |
project store                          history
  content-addressed objects,             count, resident-byte and
  immutable generations,                 spill-byte ceilings
  HEAD replaced only after verify+fsync
        |
engine/render (guarded GEGL/babl through GI): proxy pyramid, bounded tiles
        |
export: deterministic presets + provenance sidecars
ops:    typed progress and cancellation; provider ports, 0/2 production
```

A project is a directory of content-addressed objects and immutable
generations; every edit verb validates a command against the current document,
writes new objects and one new generation, and replaces `HEAD` atomically after
verifying it from disk. Source pixels are never rewritten, and adjustments,
masks, text and groups stay editable. The native image engine (GEGL and babl,
reached through Python GI from Debian packages) sits behind one guarded runtime
module, so the rest of the core imports no native code. Model-backed operations
reach the document only through a separately validated command, and no
production provider is shipped. The 16 architecture invariants and where the
code enforces them are in [ARCHITECTURE-INVARIANTS.md](ARCHITECTURE-INVARIANTS.md).

## Documentation

- [docs/command-surface.md](docs/command-surface.md): every verb, JSON output,
  exit statuses and the readiness probe.
- [docs/admission-boundary.md](docs/admission-boundary.md): what is delivered
  here and what stays blocked or owned elsewhere.
- [docs/development.md](docs/development.md): requirements, the gated `make`
  targets, the qualification verifier and the publication boundary.
- [ARCHITECTURE-INVARIANTS.md](ARCHITECTURE-INVARIANTS.md): the 16 invariants
  and their enforcement anchors.
- [QUALIFICATION.md](QUALIFICATION.md): the external qualification gates.
- [REVIEW-HANDOFF.md](REVIEW-HANDOFF.md): the independent review packet.
- [PUBLICATION.md](PUBLICATION.md): the publication rules as written.
- [CHANGELOG.md](CHANGELOG.md) and
  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Related repositories

- [kilix](https://github.com/itsmygithubacct/kilix#readme): carries the Kilix
  for Windows WSL catalog entry for Image Shop.
- [kilix-background-remover](https://github.com/itsmygithubacct/kilix-background-remover#readme):
  the F108 cutout provider candidate, whose installed provider port serves this
  core's editable-mask operation.
- [kilix-tui-utils](https://github.com/itsmygithubacct/kilix-tui-utils#readme)
  and [kilix-content](https://github.com/itsmygithubacct/kilix-content#readme):
  where the shell surface and catalog entry would live; neither is delivered.

## Part of Plebian-OS

Kilix Image Shop is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
It does not ship in Plebian-OS; only the Kilix for Windows preview's WSL
catalog lists its core commands. The map of every repository and how it ships
is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE) (Copyright 2026
itsmygithubacct). Third-party populations are listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
