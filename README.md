# edk2-cix (codgician)

[![Build Orion O6 firmware](https://github.com/codgician/edk2-cix/actions/workflows/orion-o6.yaml/badge.svg?branch=sky1)](https://github.com/codgician/edk2-cix/actions/workflows/orion-o6.yaml)

This fork builds UEFI firmware for the Radxa Orion O6 from Radxa's 1.3.1
release with a firmware TPM, measured boot (including a PCR0 measurement of the
firmware volume) and the related hardening. Only the Orion O6 is built and
tested.

The `sky1` branch is the default. Its submodules are:

| Path | Source |
| --- | --- |
| `src/edk2` | `sky1` of [codgician/edk2](https://github.com/codgician/edk2): edk2-stable202502 with CryptoPkg from edk2-stable202608 and CIX's changes |
| `src/edk2-platforms` | `sky1` of [codgician/edk2-platforms](https://github.com/codgician/edk2-platforms) |
| `src/edk2-non-osi` | Radxa's [edk2-non-osi](https://github.com/radxa/edk2-non-osi), unchanged |

New Radxa releases are merged into `sky1` from `main` of
[radxa-pkg/edk2-cix](https://github.com/radxa-pkg/edk2-cix).

### Build

The vendor packaging tools are AArch64 Linux binaries, so build natively on
AArch64 Linux with Nix:

```sh
git clone --recurse-submodules -b sky1 https://github.com/codgician/edk2-cix.git
cd edk2-cix/src
nix-shell ../shell.nix --run 'make DSC=edk2-platforms/Platform/Radxa/Orion/O6/O6.dsc Build/O6/RELEASE_GCC5/cix_flash_ota.bin Build/O6/RELEASE_GCC5/cix_flash_all.bin'
```

On NixOS, first point the tools in
`src/edk2-non-osi/Platform/CIX/Sky1/PackageTool/AARCH64` at the system dynamic
linker with `patchelf --set-interpreter`.

Every push to `sky1` builds the images and runs the firmware's host unit tests
in CI; the images are attached to the workflow run, and `v*` tags publish them
as a release. These images embed their build time. For a reproducible build
that also records the PCR0 value a healthy boot produces, use
`nix build github:codgician/nur-packages#edk2-cix-codgician`.

`cix_flash_ota.bin` replaces only the UEFI firmware (bootloader3). Flash it
only with a way back: Radxa's official image with `FlashUpdate.efi`, or an SPI
programmer.

### Comparing builds

`scripts/firmware.nix` builds that NUR package from committed revisions of
this repository and its submodules and keeps every module image, link map and
FV report; `scripts/fvdiff.py` compares two such builds module by module:

```sh
nix build --impure -f scripts/firmware.nix --argstr edk2 <rev> \
  --argstr platforms <rev> firmware -o base
scripts/fvdiff.py base head
```

Pull requests to `sky1` build base and head this way. The "Firmware gate"
workflow posts the comparison and a hardware checklist for the changed modules
(`.github/pr-gate/checklist.json`); it never merges.

### Upstream tracking

The "Watch upstreams" workflow checks daily the Radxa vendor trees
(`cix_p1_dev_*` branches of edk2-platforms, edk2-non-osi and edk2), the
radxa-pkg/edk2-cix release tags, new tianocore `edk2-stable*` tags and
tianocore's security advisories, and monthly Neol00/edk2-cix-unlocked as an
idea source (GPL-3.0: never copy its code). Each new range opens or updates one
issue labelled `upstream` with the commits, the files that overlap our own
changes or our FV (`.github/upstream/fv-sources.json`) and matching advisories.
The last-seen state lives on the `upstream-state` branch. When the repository
variable `UPSTREAM_TRIAGE` is `true`, a read-only Pi agent classifies the new
commits against `.github/upstream/policy.md` and a separate job posts the
validated result. Sources are configured in `.github/upstream/sources.json`;
run the check locally with
`.github/scripts/upstream_watch.py check --dry-run --wrapper . --cache /tmp/upstream --out /tmp/upstream/out`.

## Upstream

[![Release](https://github.com/radxa-pkg/edk2-cix/actions/workflows/release.yaml/badge.svg)](https://github.com/radxa-pkg/edk2-cix/actions/workflows/release.yaml)
### Build

1. `git clone --recurse-submodules https://github.com/radxa-pkg/edk2-cix.git`
2. Open in [`devcontainer`](https://code.visualstudio.com/docs/devcontainers/containers)
3. `make deb`

