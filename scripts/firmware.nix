# Build codgician/nur-packages#edk2-cix-codgician from committed revisions of
# this repository and its submodules, keeping what fvdiff.py compares.
#
#   nix build --impure -f scripts/firmware.nix \
#     --argstr edk2 <rev> --argstr platforms <rev> [--argstr nonOsi <rev>] \
#     [--argstr wrapper <rev>] [--argstr nur github:codgician/nur-packages/<rev>] \
#     firmware        # or: tests
#
# Revisions are fetched from the repositories checked out under ./src (and this
# repository for `wrapper`), so they must be committed there; branches of local
# worktrees count. Arguments left unset keep the NUR package's pins. The build is
# reproducible: stack cookies derive from the edk2 and edk2-platforms pins and
# PYTHONHASHSEED is fixed, so two builds of the same revisions are identical and
# builds of different revisions differ only where the code does (plus the
# per-module 8-byte stack cookie when a pin changes).
#
# `firmware` additionally installs every module image (modules/), link map
# (maps/), the FV reports, inf files and Guid.xref, and the source directory of
# every built module and library (fv/build-dirs.txt) for fv_sources.py.
{
  nur ? "github:codgician/nur-packages",
  root ? toString ./..,
  wrapper ? null,
  edk2 ? null,
  platforms ? null,
  nonOsi ? null,
}:
let
  flake = builtins.getFlake nur;
  system = "aarch64-linux";
  pkgs = flake.inputs.nixpkgs.legacyPackages.${system};
  inherit (pkgs) lib;
  local =
    path: rev: extra:
    builtins.fetchGit (
      {
        url = "${root}/${path}";
        inherit rev;
        allRefs = true;
      }
      // extra
    );
  base = flake.packages.${system}.edk2-cix-codgician;
  firmware = base.overrideAttrs (
    old:
    # The package locates its build glue under "${src.name}/src".
    lib.optionalAttrs (wrapper != null) { src = local "." wrapper { } // { name = "source"; }; }
    // lib.optionalAttrs (edk2 != null) { edk2 = local "src/edk2" edk2 { submodules = true; }; }
    // lib.optionalAttrs (platforms != null) { edk2Platforms = local "src/edk2-platforms" platforms { }; }
    // lib.optionalAttrs (nonOsi != null) { edk2NonOsi = local "src/edk2-non-osi" nonOsi { }; }
    // {
      postInstall = (old.postInstall or "") + ''
        mkdir -p $out/modules $out/maps $out/fv
        cp Build/O6/RELEASE_GCC5/AARCH64/*.efi $out/modules/
        find Build/O6/RELEASE_GCC5/AARCH64 -path '*/OUTPUT/*.map' -exec cp {} $out/maps/ \;
        cp Build/O6/RELEASE_GCC5/FV/*.Fv.txt Build/O6/RELEASE_GCC5/FV/Guid.xref Build/O6/RELEASE_GCC5/FV/*.inf $out/fv/
        (cd Build/O6/RELEASE_GCC5/AARCH64 && find . -type d -name OUTPUT | sed 's|^\./||; s|/OUTPUT$||' | LC_ALL=C sort) \
          > $out/fv/build-dirs.txt
      '';
    }
  );
in
{
  inherit firmware;
  tests = pkgs.callPackage "${flake.outPath}/tests/edk2-cix-codgician.nix" {
    edk2-cix-codgician = firmware;
  };
}
