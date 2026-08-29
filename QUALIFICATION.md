# F115 qualification handoff

The product-side OD-7 integration is implemented in 7/7 reviewable slices. The
local harness can bind an authority packet, but this repository does not create
or approve that packet and does not grade its own campaign outcomes.

## Required packet

`tools/verify_f115_qualification.py` refuses unless it can verify:

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

Success verifies carrier binding only. An independent reviewer still evaluates
the retained run records and gates.

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
- Installed-H0 adapter qualification: BLOCKED at 0/1 installed group, owned by
  Track C and the release owner.
- Provider refusals received by this repository session: 0/0.

Synthetic unit carriers receive 0/1 release-evidence credit. Retained extracted
prefix probes receive 0/1 installed-H0 qualification credit.
