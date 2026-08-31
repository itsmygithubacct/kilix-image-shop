# F115 qualification handoff

The repository contains the product-side OD-7 integration in 7/7 reviewable
slices, the frozen I1 functional core in 45/45 modules, and the 5/5-module
command surface. These are builder facts, not an independent verdict. Exact-tree
product review remains PENDING at 0/2 seats, owned by two eligible independent
reviewers designated by the release owner. The 2/2 accepted publication-record
seats graded the record, not this product or the F115 stream.

## Qualification-carrier verifier

`tools/verify_f115_qualification.py` refuses an authority packet unless it can
verify:

- 5/5 logical carriers with exact byte counts and SHA-256 identities;
- 2/2 owner freeze tokens and 48/48 direct fixture pointers;
- 15/15 completed package-input fields including 11/11 direct package rows;
- 8/8 corrected harness controls and 3/3 retained negative processor records;
- 8/8 campaign declarations; and
- 1/1 frozen 100 MP input identity.

Every referenced path must be normalized and relative to one explicit evidence
root. Symlinks, special files, duplicate JSON members, missing fields, digest
drift, provisional dispositions, and open package fields are refused.

Example invocation:

~~~sh
python3 -I tools/verify_f115_qualification.py \
  /absolute/evidence-root \
  --primary-id PRIMARY_ID \
  --primary-manifest-sha256 PRIMARY_SHA256 \
  --comparator-id COMPARATOR_ID \
  --comparator-manifest-sha256 COMPARATOR_SHA256
~~~

Success verifies 1/1 carrier bindings only. It does not approve retained run
records, installed-H0 behavior, or release admission.

## Local builder evidence

The local aggregate gate covers 5/5 phases and currently reports 258/258 unit
tests, 2/2 distribution artifacts, and 3/3 legal carriers. The wheel installs
1/1 console script, `kilix-image-shop`, whose readiness verb exits non-zero
until the complete OD-7 package group is installed. The exact committed
target must be rerun by the independent reviewer; cached or builder-only output
receives 0/1 independent-review credit.

The 15/15 acceptance-suite rows and review instructions are mapped in
`REVIEW-HANDOFF.md`. Suite 15/15 requires the owner-frozen conventional H0 and
100 MP campaign and is externally BLOCKED at 0/1 campaigns.

## Current external state

- Accepted common executable package-group schema: BLOCKED at 0/1, owned by
  Track C and the release owner.
- Final package lifecycle transaction: BLOCKED at 0/1, owned by Track C and the
  release owner.
- Owner-frozen H0 primary/comparator roles: BLOCKED at 0/2, owned by the release
  owner.
- Complete direct role manifests: BLOCKED at 0/2 manifests and 0/48 direct
  fields credited, owned by the release owner.
- Frozen measurement campaign: BLOCKED at 0/8 groups, owned by the release
  owner.
- Installed-H0 adapter qualification: BLOCKED at 0/1 installed groups, owned by
  Track C and the release owner.
- GUI toolkit selection and contained-GUI presentation: BLOCKED at 0/1, owned by
  the release owner. The command surface is delivered in this repository and
  depends on no toolkit; the contained GUI does.
- Kilix tab dispatch, `kilix-tui-utils` shell consumption and catalog entry:
  BLOCKED at 0/3 shared-repository integrations, owned by the Kilix,
  `kilix-tui-utils` and `kilix-content` streams. This repository changes 0/3 of
  them.
- Command-surface project mutations: MEASURED at 20/20 verbs and 13/13 causal
  controls. Project create/import; group, move, remove and common layer changes;
  adjustment add/replace; mask attach/replace/paint/remove and raster-selection
  conversion; revision-bound supplied pixel-stroke results; editable text add/
  replace; supplied flatten-result commit; affine transform; canvas crop; and
  selection set/clear all pass the 12/12 save transaction, 10/10 open
  validation classes and 1/1 disk readback. Mask painting binds the required
  before identity, computes the exact sparse delta at 2/4 changed tiles and 2/2
  unique tile refs in the causal carrier, and refuses stale and 0/4-tile
  changes with 0/1 HEAD changes.
  Raster-selection conversion reuses 1/1 content-addressed selection objects,
  creates 0/0 new payloads, retains the object after selection clear, and
  refuses vector selections with 0/1 HEAD changes.
  Pixel-stroke results bind 1/1 source revisions, report native-painter credit
  at 0/1, and refuse stale carriers with 0/1 HEAD changes.
  Invalid compatibility, mask, adjustment, non-recursive group removal and
  selection-crossing crop controls also preserve 0/1 HEAD changes. The
  root-to-group reducer defect found by the earlier control is closed at 1/1
  regression tests. Text mutations require 1/1 copied primary-font objects and
  1/1 declared preview assets; absent font metadata and undeclared previews
  preserve 0/1 HEAD changes. Flatten requires all supplied sources to be
  siblings and reports native-renderer credit at 0/1; its control also closes
  1/1 parser destination collisions. This is stored-document credit, not native
  decode/render/export credit.
- Native profile-backed command session: BLOCKED at 0/1 on the profile-object
  source. The project object closure deliberately excludes working and asset
  profile objects, and the package group carries `liblcms2-2` but 0/1 ICC
  profile carriers. Closing it needs either a project/G5b closure change, owned
  by the release root and Track G, or an ICC carrier in the frozen group, owned
  by Track C and the release owner.
- Frozen G5b/provider entry order: BLOCKED at 0/2 entries, owned by the release
  root and Track G.
- F108 editable-mask provider round trip: BLOCKED at 0/1, owned by F108 and the
  release root after the G5b return.
- Generation-provider round trip: BLOCKED at 0/1, owned by the release root,
  Track G, and the F115 owner.
- Exact-tree product review: PENDING at 0/2 seats, owned by two eligible
  independent reviewers designated by the release owner. Publication-record
  acceptance contributes 0/2 product-review seats.
- Provider refusals received by this repository session: 0/0.

Synthetic unit carriers receive 0/1 release-evidence credit. Retained extracted
prefix probes receive 0/1 installed-H0 qualification credit. No external gate
is converted into a local pass.
