# Development and repository status

Build requirements, the gated `make` targets, the qualification verifier, and the publication boundary as it was written. Moved from the README.

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

The builder's working copy configures 0/0 remotes and publishes by explicit URL;
a clone's remote must be the authorized private repository, which `make check`
enforces. Authorized publication is limited to `work/*` and `archive/*` refs on
the private remote `itsmygithubacct/kilix-image-shop`. Pushes to `main`,
release tags, force-pushes, history rewrites, visibility changes, package or
release-artifact publication and release-pin movement remain refused and
reserved to the owner. See [`PUBLICATION.md`](../PUBLICATION.md).
