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

The reviewed worktree must be clean at 0/0 changed paths, remain on branch
`main`, and have 0/0 configured remotes. Review authorizes 0/5 external actions:
remote creation, push, tag, package publication, and release-pin movement.

## Reproduction

From the repository root, point `UV` at the release-pinned uv 0.12.5 binary and
run:

~~~sh
git status --short
git remote
git log --oneline --decorate --reverse
make UV=/path/to/release-pinned/uv check
~~~

The builder's latest pre-review observation is 221/221 unit tests, 2/2 build
artifacts, 3/3 legal carriers, 5/5 aggregate phases, and 40/40 functional
modules. The reviewer records fresh numerators and denominators; this paragraph
is evidence provenance, not the verdict.

## Acceptance-suite map

The frozen review population is 14/14 suites:

| Suite | Primary local carriers | Reviewer result |
| --- | --- | --- |
| 1/14 immutable domain and hostile values | `test_domain_values.py`, `test_domain_layers.py`, `test_domain_document.py` | PENDING, 0/1 |
| 2/14 command, stale revision, no partial change | `test_domain_commands.py`, `test_application.py` | PENDING, 0/1 |
| 3/14 fake and OD-7 engine conformance | `test_engine_api.py`, `test_engine_runtime.py`, `test_engine_compiler.py` | PENDING, 0/1 |
| 4/14 tiled and planned composition | `test_render_compositor.py`, `test_engine_compiler.py` | PENDING, 0/1 |
| 5/14 proxy selection and invalidation | `test_render_proxy.py`, `test_render_compositor.py` | PENDING, 0/1 |
| 6/14 tile cancellation and stale suppression | `test_render_scheduler.py`, `test_render_compositor.py` | PENDING, 0/1 |
| 7/14 object, generation, and HEAD atomicity | `test_store.py` | PENDING, 0/1 |
| 8/14 save-point faults, recovery, and GC | `test_store.py` with 12/12 save points | PENDING, 0/1 |
| 9/14 history ceilings, spill, and restore | `test_history.py` | PENDING, 0/1 |
| 10/14 conventional editing semantics | `test_editing.py`, `test_domain_commands.py` | PENDING, 0/1 |
| 11/14 deterministic export and provenance | `test_export.py` | PENDING, 0/1 |
| 12/14 fake-provider lifecycle and zero mutation | `test_ops.py`, `test_application.py` | PENDING, 0/1 |
| 13/14 path, symlink, budget, privacy, and logs | `test_store.py`, `test_history.py`, `test_export.py`, `test_qualification_harness.py` | PENDING, 0/1 |
| 14/14 installed H0 conventional and 100 MP campaign | owner-supplied qualification packet | BLOCKED, 0/1; release owner and Track C |

The reviewer also checks the 16/16 architecture invariants, the exact 40/40
functional-module list asserted by `test_application.py`, the 7/7 OD-7 slices,
and the 0/2 production-provider population. Synthetic fakes may support a code
verdict but receive 0/1 installed-H0 evidence credit.

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
