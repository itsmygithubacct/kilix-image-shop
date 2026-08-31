# Independent review handoff

This packet makes the local F115 core independently reviewable without asking
the builder to grade its own work. Review status is PENDING at 0/1. The owner of
the verdict is an independent reviewer designated by the release owner.

## Target capture

The reviewer records all 4/4 target fields before review:

- reviewer identity: PENDING, 0/1;
- reviewed commit ID: PENDING, 0/1;
- reviewed tree ID: PENDING, 0/1; and
- review timestamp in UTC: PENDING, 0/1.

The reviewed worktree must be clean at 0/0 changed paths and carry only the
authorized private remote. Review authorizes 0/5 external actions: pushes to
`main`, tags, force-push or history rewrite, visibility changes, and package or
release-pin movement.

The published review target is the `work/0.2.1-f115` ref of the **private**
repository `itsmygithubacct/kilix-image-shop`. A reviewer clones it directly:

~~~sh
git clone --branch work/0.2.1-f115 \
  https://github.com/itsmygithubacct/kilix-image-shop.git
~~~

The complete gate is expected to pass in that clone exactly as it does in the
builder's working copy; the builder verified this on a fresh clone before
handing the packet over. Authenticate with a credential helper or SSH agent;
do not embed a bearer token in the remote URL. A clone legitimately has an
`origin`, so the enforced invariant is that every configured fetch and push URL
normalizes to that authorized private repository — not that no remote exists.
Both HTTPS and scp-style SSH URLs may omit or include the trailing `.git`.

Before reproduction, remove any credential-bearing clone URL and normalize the
fetch and push destinations to one credential-free authorized spelling:

~~~sh
git remote set-url origin https://github.com/itsmygithubacct/kilix-image-shop.git
git remote set-url --push origin https://github.com/itsmygithubacct/kilix-image-shop.git
~~~

## Reproduction

From the repository root, point `UV` at the release-pinned uv 0.12.5 binary.
The complete gate also requires the release hygiene tool `hygiene-scan` on
`PATH`; on this release host it is installed at `~/bin/hygiene-scan`, so prepend
`~/bin` when it is not already present. If it is absent, the hygiene recipe
exits 127; GNU Make reports `Error 127` and returns nonzero after printing
`hygiene-scan not found on PATH; see REVIEW-HANDOFF.md`. Then run:

~~~sh
git status --short
git remote
git log --oneline --decorate --reverse
make UV=/path/to/release-pinned/uv check
.venv/bin/kilix-image-shop version
.venv/bin/kilix-image-shop doctor
~~~

The builder's latest pre-review observation is 255/255 unit tests, 2/2 build
artifacts, 3/3 legal carriers, 5/5 aggregate phases, and 45/45 functional
modules. The two command invocations are the installed console-script path:
`version` exits 0, and `doctor` exits 3 on any host without the complete OD-7
package group. Neither invocation starts the engine, and neither converts a
local probe into installed-H0 evidence. The reviewer records fresh numerators
and denominators; this paragraph is evidence provenance, not the verdict.

## Acceptance-suite map

The frozen review population is 15/15 suites:

| Suite | Primary local carriers | Reviewer result |
| --- | --- | --- |
| 1/15 immutable domain and hostile values | `test_domain_values.py`, `test_domain_layers.py`, `test_domain_document.py` | PENDING, 0/1 |
| 2/15 command, stale revision, no partial change | `test_domain_commands.py`, `test_application.py` | PENDING, 0/1 |
| 3/15 fake and OD-7 engine conformance | `test_engine_api.py`, `test_engine_runtime.py`, `test_engine_compiler.py` | PENDING, 0/1 |
| 4/15 tiled and planned composition | `test_render_compositor.py`, `test_engine_compiler.py` | PENDING, 0/1 |
| 5/15 proxy selection and invalidation | `test_render_proxy.py`, `test_render_compositor.py` | PENDING, 0/1 |
| 6/15 tile cancellation and stale suppression | `test_render_scheduler.py`, `test_render_compositor.py` | PENDING, 0/1 |
| 7/15 object, generation, and HEAD atomicity | `test_store.py` | PENDING, 0/1 |
| 8/15 save-point faults, recovery, and GC | `test_store.py` with 12/12 save points | PENDING, 0/1 |
| 9/15 history ceilings, spill, and restore | `test_history.py` | PENDING, 0/1 |
| 10/15 conventional editing semantics | `test_editing.py`, `test_domain_commands.py` | PENDING, 0/1 |
| 11/15 deterministic export and provenance | `test_export.py` | PENDING, 0/1 |
| 12/15 fake-provider lifecycle and zero mutation | `test_ops.py`, `test_application.py` | PENDING, 0/1 |
| 13/15 path, symlink, budget, privacy, and logs | `test_store.py`, `test_history.py`, `test_export.py`, `test_qualification_harness.py` | PENDING, 0/1 |
| 14/15 command surface, readiness probe and maintenance verbs | `test_cli.py` | PENDING, 0/1 |
| 15/15 installed H0 conventional and 100 MP campaign | owner-supplied qualification packet | BLOCKED, 0/1; release owner and Track C |

The reviewer also checks the 16/16 architecture invariants committed in
`ARCHITECTURE-INVARIANTS.md` (each row names its enforcing module and
acceptance suite), the exact 45/45 functional-module list asserted by
`test_application.py`, the 7/7 OD-7 slices, the 5/5 command-surface modules with
their 0/1 toolkit imports, and the 0/2 production-provider population. Synthetic
fakes may support a code verdict but receive 0/1 installed-H0 evidence credit.

The command surface deliberately stops short of native pixel work. It carries
0/1 rendered exports and 0/1 engine starts, while stored-document mutations are
measured at 14/14 verbs with 8/8 causal controls. Creation/import, layer-tree,
adjustment, mask, transform, crop and selection commits all use validated
commands, the 12/12 save transaction, 10/10 open validation classes and 1/1
disk readback. The contained GUI, TUI shell and Kilix tab dispatch remain
outside this repository at their own gates.

## Required independent disposition

The independent reviewer supplies all 5/5 disposition fields:

1. `1/5` reviewed commit and tree identities;
2. `2/5` fresh command transcript with a denominator on every count;
3. `3/5` findings with severity, file/line, and closure state;
4. `4/5` one verdict: `ACCEPT`, `ACCEPT-WITH-BLOCKERS`, or `REJECT`; and
5. `5/5` explicit confirmation that external blocked gates received 0/1 local
   pass credit.

Until those 5/5 fields exist, independent review remains 0/1 and this
repository makes no release-admission claim.
