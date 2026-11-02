#!/usr/bin/env python3
"""Compare two firmware builds produced by scripts/firmware.nix, module by module.

    fvdiff.py [--json OUT] <baseline-store-path> <candidate-store-path>

Reports the FV file set and order, DEPEX changes, modules whose code changed
beyond the per-build stack cookie (the 8 bytes of __stack_chk_guard located
through the module's link map; everything else must match exactly), FFS
payloads that changed outside the module images (AML and other raw sections,
or whole files without a built image such as prebuilt drivers), PCR0 and the
FVMAIN_COMPACT usage. --json also writes the result as JSON for the
pull-request gate. Exit status 1 when the file set, order or a DEPEX changed.
"""
import argparse
import json
import os
import re
import struct
import sys
import uuid

DEPEX_OPS = {0: "BEFORE", 1: "AFTER", 2: "PUSH", 3: "AND", 4: "OR", 5: "NOT",
             6: "TRUE", 7: "FALSE", 8: "END", 9: "SOR"}
FIRMWARE = "share/edk2/radxa/orion-o6"


def guid_names(root):
    names = {}
    path = os.path.join(root, "fv", "Guid.xref")
    if os.path.exists(path):
        for line in open(path, errors="replace"):
            parts = line.split()
            if len(parts) >= 2 and re.match(r"^[0-9A-Fa-f-]{36}$", parts[0]):
                names[parts[0].upper()] = parts[1]
    return names


def cookie_offset(root, module, image):
    """File offset of the module's __stack_chk_guard, False when the link map
    shows the module has none (any difference is then real), None without a map."""
    path = os.path.join(root, "maps", module[:-len(".efi")] + ".map")
    if not os.path.exists(path):
        return None
    match = re.search(r"^\s+0x([0-9a-f]+)\s+__stack_chk_guard$", open(path, errors="replace").read(), re.M)
    if not match:
        return False
    rva = int(match.group(1), 16)
    pe = struct.unpack_from("<I", image, 0x3C)[0]
    count = struct.unpack_from("<H", image, pe + 6)[0]
    table = pe + 24 + struct.unpack_from("<H", image, pe + 20)[0]
    for i in range(count):
        vsize, va, rawsize, raw = struct.unpack_from("<IIII", image, table + 40 * i + 8)
        if va <= rva < va + min(vsize, rawsize):
            return raw + rva - va
    return None


def parse_fv(path):
    data = open(path, "rb").read()
    length = struct.unpack_from("<Q", data, 0x20)[0]
    ext = struct.unpack_from("<H", data, 0x34)[0]
    off = (ext + struct.unpack_from("<I", data, ext + 16)[0] + 7) & ~7
    files, order = {}, []
    while off + 24 <= length and data[off:off + 24] != b"\xff" * 24:
        name = str(uuid.UUID(bytes_le=data[off:off + 16])).upper()
        ftype = data[off + 18]
        size = int.from_bytes(data[off + 20:off + 23], "little")
        depex = None
        other = b""
        if ftype != 0xF0:
            sec = off + 24
            while sec + 4 <= off + size:
                ssize = int.from_bytes(data[sec:sec + 3], "little")
                if ssize < 4:
                    break
                stype = data[sec + 3]
                if stype == 0x13:
                    depex = data[sec + 4:sec + ssize]
                elif stype not in (0x10, 0x12, 0x14, 0x15):
                    # RAW/freeform payloads such as AML; PE/TE images are
                    # compared under modules/, UI and version are names.
                    other += bytes([stype]) + data[sec + 4:sec + ssize]
                sec = (sec + ssize + 3) & ~3
            order.append(name)
        files[name] = (data[off + 24:off + size], depex, other)
        off = (off + size + 7) & ~7
    return files, order


def decode_depex(raw, names):
    if raw is None:
        return "none"
    out, i = [], 0
    while i < len(raw):
        op = raw[i]
        if op in (0, 1, 2):
            guid = str(uuid.UUID(bytes_le=raw[i + 1:i + 17])).upper()
            out.append(f"{DEPEX_OPS[op]} {names.get(guid, guid)}")
            i += 17
        else:
            out.append(DEPEX_OPS.get(op, hex(op)))
            i += 1
    return " ".join(out)


def compact_usage(root):
    path = os.path.join(root, "fv", "FVMAIN_COMPACT.Fv.txt")
    if not os.path.exists(path):
        return None
    text = open(path).read()
    total = re.search(r"EFI_FV_TOTAL_SIZE = (0x[0-9a-fA-F]+)", text)
    taken = re.search(r"EFI_FV_TAKEN_SIZE = (0x[0-9a-fA-F]+)", text)
    if not (total and taken):
        return None
    return int(taken.group(1), 16), int(total.group(1), 16)


def pcr0_values(root):
    path = os.path.join(root, FIRMWARE, "pcr0")
    if not os.path.exists(path):
        return {}
    return dict(line.strip().split(":", 1) for line in open(path) if ":" in line)


def compare(a_root, b_root):
    names = {**guid_names(a_root), **guid_names(b_root)}
    a_files, a_order = parse_fv(os.path.join(a_root, FIRMWARE, "FVMAIN.Fv"))
    b_files, b_order = parse_fv(os.path.join(b_root, FIRMWARE, "FVMAIN.Fv"))
    result = {
        "files": [len(a_order), len(b_order)],
        "added": [names.get(g, g) for g in b_order if g not in a_files],
        "removed": [names.get(g, g) for g in a_order if g not in b_files],
        "order_identical": [g for g in a_order if g in b_files] == [g for g in b_order if g in a_files],
        "depex_changed": [],
        "modules": None,
        "ffs_changed": [],
        "pcr0": [pcr0_values(a_root), pcr0_values(b_root)],
        "compact": [],
    }
    for g in a_order:
        if g in b_files and a_files[g][1] != b_files[g][1]:
            result["depex_changed"].append({
                "name": names.get(g, g),
                "a": decode_depex(a_files[g][1], names),
                "b": decode_depex(b_files[g][1], names),
            })

    a_mod, b_mod = os.path.join(a_root, "modules"), os.path.join(b_root, "modules")
    module_names = set()
    if os.path.isdir(a_mod) and os.path.isdir(b_mod):
        identical = cookie = 0
        changed = []
        for name in sorted(set(os.listdir(a_mod)) | set(os.listdir(b_mod))):
            module_names.add(name.removesuffix(".efi"))
            pa, pb = os.path.join(a_mod, name), os.path.join(b_mod, name)
            if not (os.path.exists(pa) and os.path.exists(pb)):
                changed.append({"name": name, "detail": "only in " + ("B" if os.path.exists(pb) else "A")})
                continue
            x, y = open(pa, "rb").read(), open(pb, "rb").read()
            if x == y:
                identical += 1
                continue
            diff = sum(1 for p, q in zip(x, y) if p != q) + abs(len(x) - len(y))
            ca, cb = cookie_offset(a_root, name, x), cookie_offset(b_root, name, y)
            if len(x) == len(y) and ca and ca == cb and \
                    x[:ca] + x[ca + 8:] == y[:cb] + y[cb + 8:]:
                cookie += 1
            elif ca is False and cb is False:
                changed.append({"name": name, "detail": f"{diff} bytes, size {len(x)} -> {len(y)} (no stack cookie)"})
            elif ca is None or cb is None:
                changed.append({"name": name, "detail": f"{diff} bytes, no link map to rule out a code change"})
            else:
                changed.append({"name": name, "detail": f"{diff} bytes, size {len(x)} -> {len(y)}"})
        result["modules"] = {"identical": identical, "cookie_only": cookie, "changed": changed}
    # Images are compared under modules/; also compare every FFS's other
    # sections (AML and other raw payloads), or the whole file when there is
    # no module image (prebuilt drivers, raw files). Only built images carry
    # a stack cookie, so any difference here is real.
    for g in a_order:
        if g not in b_files:
            continue
        name = names.get(g, g)
        whole = name not in module_names
        x, y = (a_files[g][0], b_files[g][0]) if whole else (a_files[g][2], b_files[g][2])
        diff = sum(1 for p, q in zip(x, y) if p != q) + abs(len(x) - len(y))
        if diff:
            result["ffs_changed"].append({
                "name": name,
                "what": "file" if whole else "raw sections",
                "detail": f"{diff} bytes, size {len(x)} -> {len(y)}",
            })

    for root in (a_root, b_root):
        usage = compact_usage(root)
        result["compact"].append({"taken": usage[0], "total": usage[1]} if usage else None)
    result["failed"] = bool(result["added"] or result["removed"] or result["depex_changed"]) \
        or not result["order_identical"]
    return result


def render(result):
    lines = [
        f"FV files: {result['files'][0]} -> {result['files'][1]}; added {result['added'] or 'none'}; "
        f"removed {result['removed'] or 'none'}; common order identical: {result['order_identical']}"
    ]
    for depex in result["depex_changed"]:
        lines += [f"DEPEX changed: {depex['name']}", f"  A: {depex['a']}", f"  B: {depex['b']}"]
    modules = result["modules"]
    if modules is not None:
        lines.append(f"modules: {modules['identical']} identical, {modules['cookie_only']} differ only by "
                     f"the stack cookie, {len(modules['changed'])} changed")
        lines += [f"  {m['name']}: {m['detail']}" for m in modules["changed"]]
    lines += [f"  FFS {f['name']} {f['what']}: {f['detail']}" for f in result["ffs_changed"]]
    for label, pcr0, usage in zip("AB", result["pcr0"], result["compact"]):
        line = f"sha256:{pcr0['sha256']}" if "sha256" in pcr0 else "no pcr0"
        room = (f" FVMAIN_COMPACT {usage['taken']:#x}/{usage['total']:#x} "
                f"({usage['total'] - usage['taken']} bytes free)") if usage else ""
        lines.append(f"{label}: {line}{room}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", metavar="OUT", help="also write the comparison as JSON")
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    args = parser.parse_args()
    result = compare(args.baseline, args.candidate)
    print(render(result))
    if args.json:
        with open(args.json, "w") as out:
            json.dump(result, out, indent=2)
            out.write("\n")
    sys.exit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
