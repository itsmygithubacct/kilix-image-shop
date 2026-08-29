# Third-party notices

This ledger separates 4/4 dependency populations. `LOCKED` means the exact
identity is present in the committed build lock. `OPEN` means the dependency is
planned but its complete release transaction or licence carrier is not yet
accepted. An open population is not described as complete.

## 1. Python build and test distributions

Status: `LOCKED`. The committed `uv.lock` contains 6/6 package records: the
1/1 local project record and the following 5/5 third-party build records. The
installed application has 0/0 Python runtime dependencies and 0/0 third-party
test dependencies.

`Wheel` and `sdist` identify the exact upstream artifacts accepted into the
offline build cache. Each upstream wheel carries the stated licence text; the
5/5 distributions are build-only and are not copied into the Kilix wheel or
source distribution.

| Distribution | Locked artifacts (SHA-256) | Source | Licence | Role | Upstream licence carrier (SHA-256) |
| --- | --- | --- | --- | --- | --- |
| hatchling 1.27.0 | Wheel `d3a2f3567c4f926ea39849cdf924c7e99e6686c9c8e288ae1037c8fa2a5d937b`<br>sdist `971c296d9819abb3811112fc52c7a9751c8d381898f36533bb16f9791e941fd6` | Python Package Index | MIT | Direct build backend | `LICENSE.txt` (`7f143a8127ad4873862d70854b5bd2abd0085aa73e64fd2b08704a3b9f5c07fc`) |
| packaging 26.3 | Wheel `d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c`<br>sdist `94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79` | Python Package Index | Apache-2.0 OR BSD-2-Clause | Transitive build dependency | `LICENSE`, `LICENSE.APACHE`, and `LICENSE.BSD` (`cad1ef5bd340d73e074ba614d26f7deaca5c7940c3d8c34852e65c4909686c48`, `0d542e0c8804e39aa7f37eb00da5a762149dc682d7829451287e11b938e94594`, `b70e7e9b742f1cc6f948b34c16aa39ffece94196364bc88ff0d2180f0028fac5`) |
| pathspec 1.1.1 | Wheel `a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189`<br>sdist `17db5ecd524104a120e173814c90367a96a98d07c45b2e10c2f3919fff91bf5a` | Python Package Index | MPL-2.0 | Transitive build dependency | `LICENSE` (`fab3dd6bdab226f1c08630b1dd917e11fcb4ec5e1e020e2c16f83a0a13863e85`) |
| pluggy 1.6.0 | Wheel `e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746`<br>sdist `7dcc130b76258d33b90f61b658791dede3486c3e6bfb003ee5c9bfb396dd22f3` | Python Package Index | MIT | Transitive build dependency | `LICENSE` (`d6b65e6c213a5d0b577911d34d6e5949b9f59d76c238c5071a2f3fc16cfb2606`) |
| trove-classifiers 2026.6.1.19 | Wheel `ab4c4ec93cc4a4e7815fa759906e05e6bb3f2fbd92ea0f897288c6a43efd15b3`<br>sdist `c5132b4b61a829d11cfbd2d72e97f20a45ed6edb95e45c5efdeb5e00836b2745` | Python Package Index | Apache-2.0 | Transitive build dependency | `LICENSE` (`c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`) |

## 2. Native lazy image-engine closure

Status: `OPEN`. Stable group ID: `plebian.f115.image-engine`. OD-7 selects GEGL
`1:0.4.62-2+deb13u2` and babl `1:0.1.114-2`. The exact 11/11 direct package
members are known, but the final target-relative closure, SBOM, complete licence
ledger, and accepted transaction remain open. No native package is installed by
this Python project.

## 3. Codec, colour, and font dependencies

Status: `OPEN`. The selected direct operating-system set currently names
`libjpeg62-turbo`, `libpng16-16t64`, `libtiff6`, `libwebp7`, and `liblcms2-2`
at 5/5 members. The final resolved codec/colour inventory and the GUI/font
inventory remain open. No font dependency is selected by this skeleton.

## 4. Contract and model-operation carriers

Status: `NOT SHIPPED`. This skeleton contains 0/1 contract carriers, 0/1 model
runtimes, and 0/1 operation adapters. Their exact identities, licences, and
carried texts enter only in later separately authorized changes.
