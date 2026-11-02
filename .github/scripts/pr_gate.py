#!/usr/bin/env python3
"""Write the pull-request gate report from two builds.

    pr_gate.py --fvdiff fvdiff.json --fvdiff-text fvdiff.txt --pins pins.json
               [--fv-sources-head FILE] [--checklist FILE] > report.md

The hardware checklist comes from .github/pr-gate/checklist.json: rules whose
`match` globs name a module or FFS file that changed beyond the stack cookie,
was added or removed, or changed its DEPEX; `when` rules on PCR0, the FV file
structure and FVMAIN_COMPACT usage; and the checks of every flash.
"""
import argparse
import fnmatch
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKLIST = os.path.join(HERE, "..", "pr-gate", "checklist.json")
FV_SOURCES = os.path.join(HERE, "..", "upstream", "fv-sources.json")
MARKER = "<!-- pr-gate -->"


def changed_names(diff):
    names = set(diff["added"]) | set(diff["removed"]) | {d["name"] for d in diff["depex_changed"]}
    names |= {f["name"] for f in diff["ffs_changed"]}
    if diff["modules"]:
        names |= {m["name"].removesuffix(".efi") for m in diff["modules"]["changed"]}
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fvdiff", required=True)
    parser.add_argument("--fvdiff-text", required=True)
    parser.add_argument("--pins", required=True)
    parser.add_argument("--fv-sources-head")
    parser.add_argument("--checklist", default=CHECKLIST)
    args = parser.parse_args()
    diff = json.load(open(args.fvdiff))
    pins = json.load(open(args.pins))
    checklist = json.load(open(args.checklist))
    names = changed_names(diff)
    base_pcr0, head_pcr0 = (p.get("sha256", "") for p in diff["pcr0"])
    base_compact, head_compact = diff["compact"]

    def free(compact):
        return compact["total"] - compact["taken"] if compact else 0

    conditions = {
        "pcr0_changed": base_pcr0 != head_pcr0,
        "structure_changed": diff["failed"],
        "compact_changed": base_compact != head_compact,
    }
    values = {"pcr0_sha256": head_pcr0, "compact_free": free(head_compact), "compact_free_base": free(base_compact)}

    out = [MARKER, "## Firmware gate", ""]
    out += ["| Tree | Base | Head |", "| --- | --- | --- |"]
    out += [f"| `{name}` | `{pins['base'][name][:10]}` | `{pins['head'][name][:10]}` |" for name in pins["head"]]
    out += ["", "Both sides were built reproducibly with `scripts/firmware.nix` "
            f"(NUR `{pins.get('nur', '?')}`) and compared with `scripts/fvdiff.py`.", ""]
    out += ["<details open><summary>fvdiff</summary>", "", "```", open(args.fvdiff_text).read().rstrip(), "```",
            "", "</details>", ""]
    verdict = []
    if diff["failed"]:
        verdict.append("the FV file set, order or a DEPEX changed: explain every entry")
    if head_compact and head_compact["total"] != 0x1f0000:
        verdict.append(f"FVMAIN_COMPACT is {head_compact['total']:#x}, not 0x1f0000")
    if base_compact and head_compact:
        verdict.append(f"FVMAIN_COMPACT free space {free(base_compact)} -> {free(head_compact)} bytes "
                       f"({free(head_compact) - free(base_compact):+d})")
    out += ["**Notes:** " + "; ".join(verdict) + ".", ""] if verdict else []
    if args.fv_sources_head:
        committed = json.load(open(FV_SOURCES))
        head = json.load(open(args.fv_sources_head))
        if committed != head:
            out += ["`.github/upstream/fv-sources.json` no longer matches the head build; regenerate it with "
                    "`scripts/fv_sources.py <head-build> > .github/upstream/fv-sources.json` so upstream-watch "
                    "matches tianocore changes against the current FV.", ""]

    out += ["## Hardware checklist after flashing the head build", ""]
    sections = [checklist["always"]]
    sections += [w for w in checklist["when"] if conditions.get(w["if"])]
    for rule in checklist["rules"]:
        hits = sorted(n for n in names if any(fnmatch.fnmatchcase(n, p) for p in rule["match"]))
        if hits:
            sections.append({**rule, "title": f"{rule['title']} ({', '.join(hits)})"})
    unmatched = sorted(n for n in names if not any(fnmatch.fnmatchcase(n, p)
                                                   for r in checklist["rules"] for p in r["match"]))
    for section in sections:
        out += [f"### {section['title']}", ""]
        out += [f"- [ ] {check.format(**values)}" for check in section["checks"]]
        out.append("")
    if unmatched:
        out += ["### Changed modules without a mapped check", "",
                f"- [ ] Exercise the function of: {', '.join(unmatched)} (add a rule to "
                "`.github/pr-gate/checklist.json`).", ""]
    if not names and not conditions["pcr0_changed"]:
        out += ["The firmware volumes are identical; no module-specific hardware checks.", ""]
    out.append("This gate never merges; review and flash manually.")
    print("\n".join(out))


if __name__ == "__main__":
    main()
