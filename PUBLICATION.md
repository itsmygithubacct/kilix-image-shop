# Publication status

This repository is published to one **private** remote, and only under the
narrow authority described here. Everything not listed as authorized remains
refused.

## Authorized

- Publication of `work/*` and `archive/*` refs to the private remote
  `itsmygithubacct/kilix-image-shop`, on a clean hygiene gate, with author and
  committer equal to `itsmygithubacct
  <itsmygithubacct@users.noreply.github.com>` on every commit in the pushed
  range.

## Refused, and reserved to the owner every time

- Pushes to `main` on the remote.
- Release tags, force-pushes and history rewrites.
- Making this repository public, or any other visibility change.
- Package publication, release-artifact publication and release-pin movement.
- Publishing model or weight artifacts.

Any of these requires separate written owner approval for its exact target and
bytes. A push that lands a `work/*` ref is not an approval of anything else,
and it is not a release-admission claim: this repository still carries no
independent acceptance of its own work.

## Working-copy boundary

The working copy configures 0/0 remotes on purpose. Publication uses an
explicit URL at push time, so no ambient remote exists that an unrelated
command could push to by accident. `make check` enforces the 0/0 configured
remotes as part of its hygiene phase.
