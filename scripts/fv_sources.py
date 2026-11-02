#!/usr/bin/env python3
"""List the source directories that end up in the firmware volumes.

    fv_sources.py <firmware-store-path> > .github/upstream/fv-sources.json

Reads a build from scripts/firmware.nix: the FFS files named in fv/FVMAIN.inf
and fv/FVMAIN_COMPACT.inf, the link map of every module (which names the
libraries linked into it) and fv/build-dirs.txt (the build directory of every
module, which locates FFS files without a map such as ACPI tables). Paths are
relative to the package roots (edk2, edk2-platforms, edk2-non-osi), the form
upstream changes use. Prebuilt binaries have no source directory.
"""
import json
import os
import re
import sys

BUILD = re.compile(r"/Build/O6/RELEASE_GCC5/AARCH64/(\S+?)/[^/\s]+/(?:OUTPUT|DEBUG)/([^/\s()]+)\.(?:lib|dll)\b")


def fv_files(root):
    files = {}
    for fv in ("FVMAIN", "FVMAIN_COMPACT"):
        for line in open(os.path.join(root, "fv", f"{fv}.inf")):
            match = re.search(r"/Ffs/[0-9A-Fa-f-]{36}([^/]+)/", line)
            if match and match.group(1) not in ("FVMAIN", "FVMAIN_COMPACT"):
                files.setdefault(match.group(1), fv)
    return files


def build_dirs(root):
    """Map a module or library base name to its source directory."""
    dirs = {}
    path = os.path.join(root, "fv", "build-dirs.txt")
    if os.path.exists(path):
        for line in open(path):
            source, _, base = line.strip().rpartition("/")
            if source:
                dirs.setdefault(base, set()).add(source)
    return dirs


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    dirs = build_dirs(root)
    modules = {}
    for name, fv in sorted(fv_files(root).items()):
        own, libraries = set(), set()
        map_path = os.path.join(root, "maps", f"{name}.map")
        if os.path.exists(map_path):
            for source, base in set(BUILD.findall(open(map_path, errors="replace").read())):
                (own if base == name else libraries).add(source)
        if not own:
            own = dirs.get(name, set())
        modules[name] = {
            "fv": fv,
            "module": sorted(own),
            "libraries": sorted(libraries - own),
        }
    union = sorted({d for m in modules.values() for d in m["module"] + m["libraries"]})
    json.dump({"modules": modules, "dirs": union}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
