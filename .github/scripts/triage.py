#!/usr/bin/env python3
"""Assemble the upstream triage prompt and turn the agent's answer into comments.

    triage.py prompt --evidence DIR --state DIR
    triage.py render --evidence DIR --result FILE --issues FILE --out DIR

`prompt` copies the evidence written by `upstream_watch.py check` (OUT/triage)
to STATE/triage and writes STATE/prompt.md: the fixed instructions, the policy
reference and an index of the ranges. Upstream text stays in the evidence
files; the prompt itself contains only identifiers this repository computed.

`render` validates STATE/result/triage.json against the evidence (known range
ids and shas, fixed classes, invariant numbers, bounded plain text) and writes
one Markdown comment per issue to OUT/<issue-number>.md. Anything that does not
validate is dropped and reported as unclassified, so a misled agent cannot post
arbitrary content.
"""
import argparse
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from upstream_watch import md  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT = os.path.join(HERE, "..", "upstream", "triage-prompt.md")
CLASSES = ("already-in-tree", "security", "functional", "conflicts", "irrelevant")
INVARIANTS = range(1, 11)


def load_ranges(evidence):
    ranges = {}
    for name in sorted(os.listdir(evidence)):
        path = os.path.join(evidence, name, "range.json")
        if os.path.exists(path):
            data = json.load(open(path))
            ranges[data["id"]] = (name, data)
    return ranges


def cmd_prompt(args):
    ranges = load_ranges(args.evidence)
    target = os.path.join(args.state, "triage")
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree(args.evidence, target)
    os.makedirs(os.path.join(args.state, "result"), exist_ok=True)
    lines = [open(PROMPT).read().rstrip(), "", "## Ranges", ""]
    for range_id, (directory, data) in ranges.items():
        lines.append(f"- `{range_id}` ({data['kind']} of {data['repo']}, `{data['ref']}` at `{data['tip'][:12]}`): "
                     f"{len(data['to_classify'])} commits to classify; evidence in "
                     f"`{os.path.relpath(os.path.join(target, directory), args.root)}/`")
    with open(os.path.join(args.state, "prompt.md"), "w") as out:
        out.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def clean(text, limit):
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r"https?://\S+", "[link removed]", text)
    return md(text[:limit])


def cmd_render(args):
    ranges = load_ranges(args.evidence)
    issues = json.load(open(args.issues))
    try:
        result = json.load(open(args.result))
        answered = {r["id"]: r for r in result.get("ranges", []) if isinstance(r, dict) and "id" in r}
    except (OSError, ValueError, AttributeError) as error:
        print(f"invalid triage result: {error}", file=sys.stderr)
        answered = {}
    os.makedirs(args.out, exist_ok=True)
    for range_id, (_, data) in ranges.items():
        number = issues.get(range_id)
        if number is None:
            continue
        wanted = {c["sha"]: c for c in data["commits"] if c["sha"] in data["to_classify"]}
        answer = answered.get(range_id, {})
        rows, seen = [], set()
        for entry in answer.get("commits", []) if isinstance(answer.get("commits"), list) else []:
            if not isinstance(entry, dict):
                continue
            sha = entry.get("sha")
            classes = entry.get("classes")
            if sha not in wanted or sha in seen or not isinstance(classes, list):
                continue
            classes = [c for c in CLASSES if c in classes]
            if not classes or ("irrelevant" in classes and "already-in-tree" in classes):
                continue
            invariants = sorted({i for i in entry.get("invariants") or [] if isinstance(i, int) and i in INVARIANTS})
            seen.add(sha)
            rows.append(f"| [`{sha[:10]}`](https://github.com/{data['repo']}/commit/{sha}) "
                        f"| {md(wanted[sha]['subject'])} | {', '.join(classes)} "
                        f"| {', '.join(map(str, invariants)) or '-'} | {clean(entry.get('reason', ''), 300)} |")
        missing = [sha for sha in data["to_classify"] if sha not in seen]
        body = [
            f"<!-- upstream-triage:{range_id}@{data['tip']} -->",
            f"**AI triage** of `{data['ref']}` at `{data['tip'][:10]}` (read-only agent; advisory, "
            "verify before acting). Invariant numbers refer to `.github/upstream/policy.md`.",
            "",
        ]
        if answer.get("summary"):
            body += [f"> {clean(answer['summary'], 600)}", ""]
        if rows:
            body += ["| Commit | Subject | Classes | Invariants | Reason |", "| --- | --- | --- | --- | --- |", *rows, ""]
        if missing:
            body.append(f"Not classified ({len(missing)}): " + ", ".join(f"`{s[:10]}`" for s in missing))
        with open(os.path.join(args.out, f"{int(number)}.md"), "w") as out:
            out.write("\n".join(body)[:60000] + "\n")
        print(f"#{number}: {len(rows)} classified, {len(missing)} not classified")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    prompt = sub.add_parser("prompt")
    prompt.add_argument("--evidence", required=True)
    prompt.add_argument("--state", required=True)
    prompt.add_argument("--root", default=".")
    render = sub.add_parser("render")
    render.add_argument("--evidence", required=True)
    render.add_argument("--result", required=True)
    render.add_argument("--issues", required=True)
    render.add_argument("--out", required=True)
    args = parser.parse_args()
    {"prompt": cmd_prompt, "render": cmd_render}[args.command](args)


if __name__ == "__main__":
    main()
