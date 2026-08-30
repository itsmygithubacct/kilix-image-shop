# F115 qualification handoff

The repository contains the product-side OD-7 integration in 7/7 reviewable
slices, the frozen I1 functional core in 45/45 modules, and the 5/5-module
command surface. These are builder facts, not an independent verdict. Independent review remains PENDING at 0/1,
owned by the independent reviewer designated by the release owner.

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

The local aggregate gate covers 5/5 phases and currently reports 246/246 unit
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
- Command-surface editing verbs: BLOCKED at 0/1 on the profile-object source.
  A saved project's object closure excludes the colour-profile objects the
  application boundary must resolve before it may publish a revision, so no
  editing verb is offered rather than offering one that cannot open a
  profile-bearing project. Owned by the F115 owner as a design decision, and
  recorded rather than worked around.
- Frozen G5b/provider entry order: BLOCKED at 0/2 entries, owned by the release
  root and Track G.
- F108 editable-mask provider round trip: BLOCKED at 0/1, owned by F108 and the
  release root after the G5b return.
- Generation-provider round trip: BLOCKED at 0/1, owned by the release root,
  Track G, and the F115 owner.
- Independent repository review: PENDING at 0/1, owned by the independent
  reviewer designated by the release owner.
- Provider refusals received by this repository session: 0/0.

Synthetic unit carriers receive 0/1 release-evidence credit. Retained extracted
prefix probes receive 0/1 installed-H0 qualification credit. No external gate
is converted into a local pass.
