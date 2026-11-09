# Command surface

Every `kilix-image-shop` verb, the JSON output mode, exit statuses and the `doctor` readiness probe. Moved from the README.

The wheel installs 1/1 console script, `kilix-image-shop`. Every verb consumes
the same core values the later toolkit surfaces will consume. No verb starts
the native engine or claims a rendered pixel; the edit verbs do commit checked
document revisions through the 12/12-point generation transaction:

| Verb | Result |
| --- | --- |
| `version` | product, schema and accepted OD-7 group identity |
| `doctor` | per-component readiness for the OD-7 group, interpreter and providers |
| `project create ROOT COMPATIBILITY --width W --height H` | creates an empty canonical project and verifies its first generation from disk |
| `project info ROOT` | opens a project through all 10/10 validation classes |
| `project layers ROOT` | lists the exact layer tree selected by HEAD |
| `project verify ROOT` | re-digests every object the current generation needs |
| `project generations ROOT` | lists retained generations and marks HEAD |
| `project recover ROOT GENERATION [--apply]` | previews, and only with `--apply` selects, another generation |
| `project gc ROOT [--apply]` | previews, and only with `--apply` quarantines, unreachable objects |
| `edit import ROOT ASSET ...` | copies a bounded encoded carrier into a new validated pixel layer and generation |
| `edit pixel-stroke-result ROOT CARRIER --before-revision-id UUID ...` | commits a completed stroke carrier only against its exact source revision, with 0/1 native-painter credit |
| `edit adjustment ROOT ID --parameter NAME=JSON ...` | adds one closed non-destructive adjustment layer |
| `edit adjustment-set ROOT LAYER ID ...` | replaces one adjustment through the same validation rules |
| `edit mask ROOT LAYER MASK` | attaches or replaces a full-canvas editable foreground-alpha Y u8 mask |
| `edit mask-paint ROOT LAYER MASK --before-sha256 SHA256` | commits a stale-checked full-mask paint result and exact sparse tile delta |
| `edit mask-from-selection ROOT LAYER` | binds the active raster selection as an editable mask without copying its object |
| `edit mask-remove ROOT LAYER` | removes a mask without changing source pixels |
| `edit layer ROOT LAYER ...` | changes checked visibility, opacity, blend mode or name fields |
| `edit group`, `layer-move`, `layer-remove` | creates and explicitly restructures the layer tree |
| `edit text`, `text-set` | adds or replaces editable text with copied pinned font bytes, axes and a declared preview asset |
| `edit flatten-result` | atomically commits a supplied local flatten carrier while crediting 0/1 native renderers |
| `edit transform`, `crop` | changes checked affine/canvas geometry without implicit resampling |
| `edit selection`, `selection-clear` | sets or clears one bounded content-addressed selection |
| `edit selection-raster-result ROOT RASTER --before-sha256 SHA256` | replaces a checked vector selection with exact Y u8 samples, with 0/1 native-rasterizer credit |
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
