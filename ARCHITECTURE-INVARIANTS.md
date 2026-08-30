# Architecture invariants

The F115 I1 core carries 16/16 non-negotiable architecture invariants. This file
is the committed population that `REVIEW-HANDOFF.md` requires the independent
reviewer to check; each row states the invariant and where the accepted code
enforces it. The text of the 16 invariants is the frozen architecture set; the
enforcement anchors are repository paths and named acceptance tests, so the
check is mechanical rather than a subjective architecture read.

Enforcement anchors use repository-relative paths only. The acceptance-suite
numbers refer to the 14/14 suite map in `REVIEW-HANDOFF.md`.

| # | Invariant | Enforced in | Acceptance suite |
| --- | --- | --- | --- |
| 1/16 | Imported source bytes are immutable. An edit creates intent or a new content object; it never rewrites the source. | `src/kilix_image_shop/domain/assets.py`, `src/kilix_image_shop/store/objects.py` | 1/14, 7/14 |
| 2/16 | `DocumentState`, layers, adjustments, masks, selections and history entries are immutable values with stable IDs. | `src/kilix_image_shop/domain/document.py`, `domain/layers.py`, `domain/identifiers.py` | 1/14 |
| 3/16 | A command is validated before reduction and returns either one new state plus effects or no state change. | `src/kilix_image_shop/domain/commands.py`, `src/kilix_image_shop/application.py` | 2/14 |
| 4/16 | Pixel and mask payloads are content-addressed. Domain values hold digests, geometry and semantics, not mutable GEGL buffers. | `src/kilix_image_shop/store/objects.py`, `domain/assets.py` | 7/14 |
| 5/16 | Pixel, adjustment, text and group layers remain editable. Flatten is an explicit command producing a new pixel object. | `src/kilix_image_shop/domain/layers.py`, `editing/adjustments.py`, `editing/text.py` | 1/14, 10/14 |
| 6/16 | Masks are first-class `Y u8` foreground-alpha objects with geometry, origin and provenance. Attaching or replacing a mask is undoable. | `src/kilix_image_shop/editing/masking.py`, `domain/layers.py` | 10/14 |
| 7/16 | H0 uses `RGBA u16` working buffers. Float working buffers are unavailable at H0 and may be selected only by the higher-tier fit policy. | `src/kilix_image_shop/engine/formats.py`, `engine/compatibility.py` | 3/14 |
| 8/16 | Interactive zoom-out reads the proxy pyramid, never a full-resolution visible region scaled into the viewport. | `src/kilix_image_shop/render/proxy.py`, `render/compositor.py` | 5/14 |
| 9/16 | Interactive and cancellable rendering uses bounded, individually completing tiles. A long `GeglProcessor` is used in 0/1 product paths. | `src/kilix_image_shop/render/scheduler.py`, `render/compositor.py` | 6/14 |
| 10/16 | History has count, resident-byte and spill-byte ceilings. An unbounded policy is invalid configuration, not a hidden default. | `src/kilix_image_shop/history/budget.py`, `history/spill.py`, `history/stack.py` | 9/14 |
| 11/16 | A save writes immutable objects and one immutable generation, then atomically replaces `HEAD` only after verification and fsync. | `src/kilix_image_shop/store/generations.py` (`SAVE_POINT_COUNT = 12`) | 7/14, 8/14 — `tests/test_store.py::test_all_twelve_ordered_fault_points_preserve_atomic_head_semantics` |
| 12/16 | Recovery and garbage collection never delete the current head, reachable objects, imported external sources, projects or exports. | `src/kilix_image_shop/store/recovery.py`, `store/gc.py` | 8/14 |
| 13/16 | Deterministic export initializes babl with `BABL_TOLERANCE=0.0` and records the working format and engine compatibility identity. | `src/kilix_image_shop/engine/runtime.py:936-937`, `export/presets.py`, `export/provenance.py` | 11/14 |
| 14/16 | I1 has 0/1 model dependencies and 0/1 provider adapters. Its operation substrate is proven with an in-process fake port. | `src/kilix_image_shop/ops/orchestrator.py` (`zero_provider()`), `ports.py` | 12/14 — `tests/test_ops.py::test_production_registry_has_zero_providers_and_two_unavailable_views` |
| 15/16 | UI, TUI and CLI consume the same application commands and view models. Selecting the contained GUI toolkit changes 0/5 core families: domain, render, store, history and operations. | `src/kilix_image_shop/application.py`, `ops/state.py` | 2/14, 12/14 — `tests/test_application.py::test_frozen_functional_module_population_is_exactly_forty` |
| 16/16 | Cancellation or adapter failure commits 0/1 document mutations. Completed operation output enters through one separately validated command. | `src/kilix_image_shop/render/scheduler.py`, `ops/orchestrator.py`, `domain/commands.py` | 6/14, 12/14 |

The invariant text is frozen and originates in the F115 I1 checkout-architecture
design. Any change to a module that would violate its row is an architecture
regression, independent of unit-test status.
