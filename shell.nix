{
  pkgs ? import <nixpkgs> { },
}:
let
  # acpica-tools 20260408 and later fail on the ASL printf macro used by the
  # CIX SoC DSDT; keep the last release that compiles it.
  acpica-tools = pkgs.acpica-tools.overrideAttrs (_: rec {
    version = "20250807";
    src = pkgs.fetchFromGitHub {
      owner = "acpica";
      repo = "acpica";
      tag = version;
      hash = "sha256-OY7jEirUDpzhgT9iCUYWeZmbCQl2R/agGIHXqJI/UBo=";
    };
  });
in
# BaseTools' vendored Pccts parser generator is K&R C that GCC 15 rejects.
pkgs.mkShell.override { stdenv = pkgs.gcc14Stdenv; } {
  packages = with pkgs; [
    acpica-tools
    bash
    bc
    coreutils
    dpkg
    git
    gnumake
    gnum4
    patchelf
    python3
    which
  ];
  # BaseTools' C utilities need libuuid headers.
  buildInputs = [ pkgs.libuuid ];
  # nixpkgs' hardening flags break EDK2's freestanding firmware build.
  hardeningDisable = [ "all" ];
}
