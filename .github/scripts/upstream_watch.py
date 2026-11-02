#!/usr/bin/env python3
"""Watch the upstreams of this firmware and report new ranges as issues.

    upstream_watch.py check   --wrapper DIR [--wrapper-ref REF] [--state FILE] --cache DIR --out DIR
                              [--dry-run] [--local-forks] [--force]
    upstream_watch.py publish --out DIR --repo OWNER/NAME
    upstream_watch.py commit-state --out DIR --repo OWNER/NAME --branch NAME --parent SHA

`check` is read-only towards every remote: it fetches the configured upstreams
(blob-less) into a local cache, compares them with the last-seen state and with
the revisions this repository pins (the submodules at --wrapper-ref), and writes
OUT/report.json, OUT/state.json, one issue description per range under
OUT/issues/ and the evidence for the triage agent under OUT/triage/. Nothing is
published; --dry-run also prints the issue bodies. Only ranges that bring
commits missing from our tree are marked for triage.

`publish` opens or updates one issue per range (found by a hidden marker) with
GH_TOKEN and records the issue numbers in OUT/issues.json. `commit-state`
commits OUT/state.json to the state branch through the API; the update is a
fast-forward from --parent, so a concurrent writer makes it fail instead of
being overwritten.
"""
import argparse
import base64
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "upstream", "sources.json")
FV_SOURCES = os.path.join(HERE, "..", "upstream", "fv-sources.json")
LABEL = "upstream"
MAX_BODY = 60000
MAX_ROWS = 150
MAX_PATCHES = 200
MAX_PATCH_BYTES = 200_000
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


# --------------------------------------------------------------------------- git


def git(*args, cwd=None, check=True, input=None):
    result = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, input=input, text=True,
                            capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def has_commit(cache, sha):
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=cache,
                          env={**GIT_ENV, "GIT_NO_LAZY_FETCH": "1"}, capture_output=True).returncode == 0


def open_cache(root, name):
    path = os.path.join(root, f"{name}.git")
    if not os.path.isdir(path):
        os.makedirs(path)
        git("init", "--quiet", "--bare", path)
        git("config", "core.repositoryformatversion", "1", cwd=path)
        git("config", "extensions.partialClone", "promisor", cwd=path)
        git("config", "gc.auto", "0", cwd=path)
    return path


def add_promisor(cache, name, url):
    """Register a blob-less promisor remote; missing blobs are fetched on demand."""
    remotes = git("remote", cwd=cache).split()
    if name in remotes:
        git("remote", "set-url", name, url, cwd=cache)
    else:
        git("remote", "add", name, url, cwd=cache)
    git("config", f"remote.{name}.promisor", "true", cwd=cache)
    git("config", f"remote.{name}.partialclonefilter", "blob:none", cwd=cache)
    # extensions.partialClone names the remote tried first for lazy fetches.
    if "promisor" not in remotes:
        git("remote", "add", "promisor", url, cwd=cache)
        git("config", "remote.promisor.promisor", "true", cwd=cache)
        git("config", "remote.promisor.partialclonefilter", "blob:none", cwd=cache)


def fetch(cache, remote, *refspecs):
    git("fetch", "--quiet", "--no-tags", "--force", "--filter=blob:none", remote, *refspecs, cwd=cache)


def ls_remote(url, kind, pattern):
    refs = {}
    for line in git("ls-remote", f"--{kind}", url).splitlines():
        sha, ref = line.split("\t")
        if ref.endswith("^{}"):
            continue
        name = ref.split("/", 2)[2]
        if fnmatch.fnmatchcase(name, pattern):
            refs[name] = sha
    return refs


def ensure_commit(cache, sha, remotes):
    if has_commit(cache, sha):
        return
    for remote in remotes:
        if remote:
            subprocess.run(["git", "fetch", "--quiet", "--no-tags", "--filter=blob:none", remote, sha],
                           cwd=cache, env=GIT_ENV, capture_output=True)
            if has_commit(cache, sha):
                return
    raise RuntimeError(f"commit {sha} is not reachable from {remotes}")


def log(cache, *range_args, files=False):
    """Commits (newest first) with subject, author, date and optionally files."""
    fmt = "%x1e%H%x1f%s%x1f%an%x1f%cI%x1f%P"
    out = git("log", "--no-merges", f"--format={fmt}", *(["--name-only"] if files else []),
              *range_args, "--", cwd=cache)
    commits = []
    for record in out.split("\x1e")[1:]:
        header, _, rest = record.partition("\n")
        sha, subject, author, date, parents = header.split("\x1f")
        commits.append({
            "sha": sha, "subject": subject, "author": author, "date": date,
            "files": [f for f in rest.splitlines() if f] if files else [],
        })
    return commits


def missing_objects(cache, oids):
    """Objects absent from the cache, checked without triggering lazy fetches."""
    if not oids:
        return []
    out = subprocess.run(["git", "cat-file", "--batch-check"], cwd=cache, input="\n".join(oids) + "\n", text=True,
                         capture_output=True, env={**GIT_ENV, "GIT_NO_LAZY_FETCH": "1"}).stdout
    return [line.split()[0] for line in out.splitlines() if line.endswith(" missing")]


def prefetch_blobs(cache, remotes, shas):
    """Fetch the blobs the given commits change in one request per remote instead of one per blob."""
    if not shas:
        return
    out = git("diff-tree", "--stdin", "-r", "--no-commit-id", "--root", cwd=cache, input="\n".join(shas) + "\n")
    oids = set()
    for line in out.splitlines():
        if line.startswith(":"):
            mode_a, mode_b, oid_a, oid_b = line[1:].split("\t", 1)[0].split()[:4]
            oids.update(o for o, m in ((oid_a, mode_a), (oid_b, mode_b)) if o.strip("0") and m != "160000")
    missing = missing_objects(cache, sorted(oids))
    for remote in remotes:
        if not missing:
            break
        # The request git sends for a lazy fetch, batched.
        subprocess.run(["git", "-c", "fetch.negotiationAlgorithm=noop", "fetch", "--quiet", remote, "--no-tags",
                        "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none", "--stdin"],
                       cwd=cache, env=GIT_ENV, input="\n".join(missing) + "\n", text=True, capture_output=True)
        missing = missing_objects(cache, missing)


def patch_ids(cache, remotes, shas):
    """Stable patch-id of each commit (git cherry's equivalence), fetching blobs in batches."""
    if not shas:
        return {}
    prefetch_blobs(cache, remotes, shas)
    diff = git("diff-tree", "--stdin", "-p", "--root", cwd=cache, input="\n".join(shas) + "\n")
    out = git("patch-id", "--stable", cwd=cache, input=diff)
    return {commit: pid for pid, commit in (line.split() for line in out.splitlines())}


def matching(cache, remotes, ids, since, head, files):
    """Commits of pending ids whose patch-id appears in since..head (only commits touching the same files)."""
    candidates = git("log", "--no-merges", "--format=%H", f"{since}..{head}", "--", *files, cwd=cache).split()
    found = set(patch_ids(cache, remotes, candidates).values())
    return {sha for sha, pid in ids.items() if pid in found}


def subjects(cache, *range_args):
    return set(git("log", "--no-merges", "--format=%s", *range_args, "--", cwd=cache).splitlines())


def cherry_picked_from(cache, *range_args):
    return set(re.findall(r"cherry picked from commit ([0-9a-f]{40})",
                          git("log", "--format=%b", *range_args, "--", cwd=cache)))


def version_key(name):
    return tuple(int(n) for n in re.findall(r"\d+", name))


# ---------------------------------------------------------------------- sources


def wrapper_pin(args, submodule=None):
    spec = f"{args.wrapper_ref}:{submodule}" if submodule else args.wrapper_ref
    return git("rev-parse", spec, cwd=args.wrapper).strip()


def local_repo(args, source):
    path = os.path.join(args.wrapper, source["submodule"]) if source.get("submodule") else args.wrapper
    return os.path.abspath(path) if os.path.exists(os.path.join(path, ".git")) else None


def fork_refs(cache, source, config):
    refs = git("for-each-ref", "--format=%(refname)", f"refs/fork/{source['id']}",
               f"refs/local/{source['id']}", cwd=cache).split()
    keep = []
    for ref in refs:
        branch = ref.split("/", 3)[3]
        if not any(fnmatch.fnmatchcase(branch, p) for p in config["fork_ignore"]):
            keep.append(ref)
    return keep


def prepare_fork(cache, source, config, args):
    remotes = []
    if source.get("fork_url"):
        add_promisor(cache, f"fork-{source['id']}", source["fork_url"])
        fetch(cache, f"fork-{source['id']}", f"+refs/heads/*:refs/fork/{source['id']}/*")
        remotes.append(f"fork-{source['id']}")
    path = local_repo(args, source)
    if args.local_forks and path:
        # Borrow the local objects (including blobs) instead of fetching them.
        objects = git("rev-parse", "--path-format=absolute", "--git-path", "objects", cwd=path).strip()
        alternates = os.path.join(cache, "objects", "info", "alternates")
        known = open(alternates).read().split() if os.path.exists(alternates) else []
        if objects not in known:
            with open(alternates, "a") as out:
                out.write(objects + "\n")
        git("fetch", "--quiet", "--no-tags", "--force", path,
            f"+refs/heads/*:refs/local/{source['id']}/*", cwd=cache)
    return remotes


def annotate_commits(cache, commits, pin, tip, refs, remotes):
    """Mark commits already in our tree and the fork branches that carry them.

    Equivalent to `git cherry`, but patch-ids are only computed for our commits
    that touch the files of the pending commits, so a blob-less cache fetches
    a few blobs instead of the whole history."""
    if not commits:
        return
    files = sorted({f for c in commits for f in c["files"]})
    ids = patch_ids(cache, remotes, [c["sha"] for c in commits])
    base = git("merge-base", pin, tip, cwd=cache).strip()
    present = matching(cache, remotes, ids, base, pin, files) if files else set()
    ours_subjects = subjects(cache, f"{tip}..{pin}")
    picked = cherry_picked_from(cache, f"{tip}..{pin}")
    carried = {}
    for ref in refs:
        since = git("merge-base", ref, tip, cwd=cache).strip()
        reachable = set(git("rev-list", f"{since}..{tip}", cwd=cache).split()) if since else set()
        on_ref = {c["sha"] for c in commits if c["sha"] not in reachable}
        on_ref |= matching(cache, remotes, ids, since, ref, files) if files and since else set()
        for c in commits:
            if c["sha"] in on_ref:
                carried.setdefault(c["sha"], []).append(ref.split("/", 3)[3])
    for c in commits:
        if c["sha"] in present:
            c["in_tree"] = "patch-id"
        elif c["sha"] in picked:
            c["in_tree"] = "cherry-pick"
        elif c["subject"] in ours_subjects:
            c["in_tree"] = "subject"
        else:
            c["in_tree"] = None
        c["in_fork_branches"] = carried.get(c["sha"], [])


def overlap(commits, ours):
    touched = {}
    for c in commits:
        for f in c["files"]:
            if f in ours:
                touched.setdefault(f, []).append(c["sha"][:10])
    return [{"path": p, "commits": s} for p, s in sorted(touched.items())]


def fv_paths(paths, dirs):
    return sorted(p for p in paths if any(p.startswith(d + "/") for d in dirs))


def check_vendor(source, prev, config, args, ctx):
    cache = open_cache(args.cache, source["cache"])
    remote = f"upstream-{source['id']}"
    add_promisor(cache, remote, source["url"])
    heads = ls_remote(source["url"], "heads", source["branches"])
    known = prev.get("refs")
    state = {"refs": dict(heads)}
    if not heads:
        return state, []
    fetch(cache, remote, *[f"+refs/heads/{b}:refs/upstream/{source['id']}/{b}" for b in heads])
    fork_remotes = prepare_fork(cache, source, config, args)
    pin = wrapper_pin(args, source["submodule"])
    ensure_commit(cache, pin, fork_remotes)
    refs = [r for r in fork_refs(cache, source, config) if git("rev-parse", r, cwd=cache).strip() != pin]
    ranges = []
    for branch, tip in sorted(heads.items(), key=lambda kv: version_key(kv[0])):
        last = (known or {}).get(branch)
        if last == tip and not args.force:
            continue
        pending = log(cache, f"{pin}..{tip}", files=True)
        annotate_commits(cache, pending, pin, tip, refs, [remote, *fork_remotes])
        forced = bool(last) and not (has_commit(cache, last) and
                                     subprocess.run(["git", "merge-base", "--is-ancestor", last, tip],
                                                    cwd=cache, env=GIT_ENV).returncode == 0)
        fresh = set(git("rev-list", "--no-merges", tip, f"^{pin}", *([f"^{last}"] if last and not forced else []),
                        cwd=cache).split())
        for c in pending:
            c["new"] = c["sha"] in fresh
        base = git("merge-base", pin, tip, cwd=cache).strip()
        ours = set(git("diff", "--name-only", base, pin, cwd=cache).split("\n")) - {""}
        missing = [c for c in pending if not c["in_tree"]]
        ranges.append({
            "id": f"{source['id']}/{branch}",
            "source": source["id"], "kind": source["kind"], "repo": source["repo"], "ref": branch,
            "pin": pin, "merge_base": base, "tip": tip, "previous": last,
            "new_branch": known is not None and last is None, "forced": forced,
            "commits": pending,
            "fork_changed_files": len(ours),
            "overlap": overlap(missing, ours),
            "advisories": ctx["advisory_refs"](pending),
            "triage": bool(source.get("triage")) and any(c["new"] and not c["in_tree"] for c in pending),
        })
    return state, ranges


def check_release_tags(source, prev, config, args, ctx):
    cache = open_cache(args.cache, source["cache"])
    remote = f"upstream-{source['id']}"
    add_promisor(cache, remote, source["url"])
    tags = ls_remote(source["url"], "tags", source["tags"])
    seen = prev.get("tags")
    if seen is None:
        seen = [t for t in tags if version_key(t) <= version_key(source["seed_seen_through"])]
    state = {"tags": sorted(set(seen) | set(tags), key=version_key)}
    new = sorted((t for t in tags if t not in seen or args.force), key=version_key)
    if args.force:
        new = new[-1:]
    if not new:
        return state, []
    ordered = sorted(tags, key=version_key)
    fetch(cache, remote, *[f"+refs/tags/{t}:refs/upstream/{source['id']}/tags/{t}" for t in tags])
    fork_remotes = prepare_fork(cache, source, config, args)
    pin = wrapper_pin(args, source.get("submodule"))
    ensure_commit(cache, pin, fork_remotes)
    refs = fork_refs(cache, source, config)
    dirs = ctx["fv"]["dirs"]
    ranges = []
    for tag in new:
        tip = git("rev-parse", f"refs/upstream/{source['id']}/tags/{tag}^{{commit}}", cwd=cache).strip()
        index = ordered.index(tag)
        before = ordered[index - 1] if index else None
        start = git("rev-parse", f"refs/upstream/{source['id']}/tags/{before}^{{commit}}", cwd=cache).strip() \
            if before else None
        commits = log(cache, tip, *([f"^{start}"] if start else []), f"^{pin}", files=True)
        base = git("merge-base", pin, tip, cwd=cache).strip()
        entry = {
            "id": f"{source['id']}/{tag}",
            "source": source["id"], "kind": source["kind"], "repo": source["repo"], "ref": tag,
            "pin": pin, "merge_base": base, "tip": tip, "previous": before, "previous_tip": start,
            "new_branch": False, "forced": False,
        }
        if source["kind"] == "stable-tags":
            # Upstream releases are large; keep the commits that touch code in our FV.
            picked = cherry_picked_from(cache, f"{base}..{pin}")
            ours_subjects = subjects(cache, f"{base}..{pin}")
            relevant = []
            for c in commits:
                c["fv_files"] = fv_paths(c["files"], dirs)
                if c["fv_files"]:
                    c["in_tree"] = ("cherry-pick" if c["sha"] in picked else
                                    "subject" if c["subject"] in ours_subjects else None)
                    c["in_fork_branches"] = []
                    c["new"] = True
                    relevant.append(c)
            ids = patch_ids(cache, [remote, *fork_remotes], [c["sha"] for c in relevant])
            files = sorted({f for c in relevant for f in c["fv_files"]})
            present = matching(cache, [remote, *fork_remotes], ids, base, pin, files) if files else set()
            for c in relevant:
                if c["sha"] in present:
                    c["in_tree"] = "patch-id"
            changed = sorted({f for c in commits for f in c["files"]})
            entry.update({
                "total_commits": len(commits),
                "changed_files": len(changed),
                "commits": relevant,
                "fv_files": fv_paths(changed, dirs),
                "advisories": ctx["advisories_for_tag"](tag, before, ctx["tag_dates"](cache, source, before, tag))
                + [a for a in ctx["advisory_refs"](relevant)],
            })
            entry["advisories"] = sorted(set(entry["advisories"]))
            entry["overlap"] = [{"path": p, "commits": [c["sha"][:10] for c in relevant if p in c["fv_files"]]}
                                for p in entry["fv_files"]]
        else:
            annotate_commits(cache, commits, pin, tip, refs, [remote, *fork_remotes])
            for c in commits:
                c["new"] = True
            ours = set(git("diff", "--name-only", base, pin, cwd=cache).split("\n")) - {""}
            entry.update({
                "commits": commits,
                "fork_changed_files": len(ours),
                "overlap": overlap([c for c in commits if not c["in_tree"]], ours),
                "submodules": submodule_bumps(cache, start or base, tip),
                "advisories": ctx["advisory_refs"](commits),
            })
        entry["triage"] = bool(source.get("triage")) and any(not c["in_tree"] for c in entry["commits"])
        ranges.append(entry)
    return state, ranges


def submodule_bumps(cache, old, new):
    bumps = []
    for line in git("diff-tree", "-r", old, new, cwd=cache).splitlines():
        meta, path = line.split("\t", 1)
        mode_a, mode_b, sha_a, sha_b, _ = meta[1:].split()
        if "160000" in (mode_a, mode_b):
            bumps.append({"path": path, "from": sha_a, "to": sha_b})
    return bumps


def check_idea(source, prev, config, args, ctx):
    last_checked = prev.get("last_checked")
    if last_checked and not args.force:
        due = datetime.fromisoformat(last_checked) + timedelta(days=source["interval_days"])
        if ctx["now"] < due:
            return prev, []
    cache = open_cache(args.cache, source["cache"])
    remote = f"upstream-{source['id']}"
    add_promisor(cache, remote, source["url"])
    heads = ls_remote(source["url"], "heads", source["branches"])
    state = {"refs": dict(heads), "last_checked": ctx["now"].isoformat(timespec="seconds")}
    fetch(cache, remote, *[f"+refs/heads/{b}:refs/upstream/{source['id']}/{b}" for b in heads])
    ranges = []
    for branch, tip in heads.items():
        last = (prev.get("refs") or {}).get(branch) or source.get("seed_tip")
        if last:
            last = git("rev-parse", "--verify", "--quiet", f"{last}^{{commit}}", cwd=cache, check=False).strip() or None
        if last == tip:
            continue
        commits = log(cache, tip, *([f"^{last}"] if last else ["-n", "50"]), files=True)
        for c in commits:
            c["in_tree"], c["in_fork_branches"], c["new"] = None, [], True
        ranges.append({
            "id": f"{source['id']}/{branch}", "source": source["id"], "kind": source["kind"],
            "repo": source["repo"], "ref": branch, "tip": tip, "previous": last,
            "new_branch": False, "forced": False, "commits": commits, "overlap": [], "advisories": [],
            "note": source.get("note"), "triage": False,
        })
    return state, ranges


# ------------------------------------------------------------------- advisories


def github_get(url, token=None):
    items = []
    while url:
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        })
        with urllib.request.urlopen(request, timeout=60) as response:
            items += json.load(response)
            links = re.findall(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link", ""))
            url = links[0] if links else None
    return items


def load_advisories(repo, fv):
    raw = github_get(f"https://api.github.com/repos/{repo}/security-advisories?state=published&per_page=100",
                     os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    names = set(fv["modules"]) | {d.split("/")[0] for d in fv["dirs"]}
    advisories = []
    for a in raw:
        text = f"{a.get('summary') or ''}\n{a.get('description') or ''}"
        vulns = a.get("vulnerabilities") or []
        mentions = sorted({d for d in fv["dirs"] if d in text} |
                          {n for n in names if re.search(rf"\b{re.escape(n)}\b", text)})
        advisories.append({
            "id": a["ghsa_id"], "cve": a.get("cve_id"), "severity": a.get("severity"),
            "summary": (a.get("summary") or "").strip(), "url": a.get("html_url"),
            "published": a.get("published_at"),
            "vulnerable": "; ".join(v.get("vulnerable_version_range") or "" for v in vulns),
            "patched": "; ".join(v.get("patched_versions") or "" for v in vulns),
            "fv_mentions": mentions,
        })
    return sorted(advisories, key=lambda a: a["published"] or "")


def covers(advisory, base_tag):
    """True/False when the vulnerable range clearly includes/excludes our base."""
    base = version_key(base_tag)[:1]
    match = re.fullmatch(r"\s*<=\s*(\d{6})\s*", advisory["vulnerable"])
    if match:
        return base <= (int(match.group(1)),)
    return None


# ------------------------------------------------------------------------ check


def cmd_check(args):
    config = json.load(open(args.config))
    fv = json.load(open(args.fv_sources))
    old_state = json.load(open(args.state)) if args.state and os.path.getsize(args.state) else {}
    old_sources = old_state.get("sources", {})
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc).replace(microsecond=0)
    os.makedirs(args.cache, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    advisories, advisory_source = [], None
    for source in config["sources"]:
        if source.get("advisories"):
            advisory_source = source
            advisories = load_advisories(source["advisories"], fv)
    by_id = {a["id"]: a for a in advisories}
    by_cve = {a["cve"]: a for a in advisories if a["cve"]}

    def advisory_refs(commits):
        found = set()
        for c in commits:
            text = c["subject"]
            for ident in re.findall(r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}|CVE-\d{4}-\d{4,}", text):
                a = by_id.get(ident) or by_cve.get(ident)
                if a:
                    found.add(a["id"])
        return sorted(found)

    def tag_dates(cache, source, before, tag):
        def date(t):
            if not t:
                return datetime.min.replace(tzinfo=timezone.utc)
            return datetime.fromisoformat(
                git("log", "-1", "--format=%cI", f"refs/upstream/{source['id']}/tags/{t}", cwd=cache).strip())
        return date(before), date(tag)

    def advisories_for_tag(tag, before, dates):
        version = tag.removeprefix("edk2-stable")
        start, end = dates
        hits = []
        for a in advisories:
            patched = re.findall(r"\d{6}(?:\.\d+)?", a["patched"])
            published = datetime.fromisoformat(a["published"]) if a["published"] else None
            if version in patched or (published and start < published <= end):
                hits.append(a["id"])
        return hits

    ctx = {"fv": fv, "now": now, "advisory_refs": advisory_refs, "tag_dates": tag_dates,
           "advisories_for_tag": advisories_for_tag}
    checkers = {"vendor-branches": check_vendor, "release-tags": check_release_tags,
                "stable-tags": check_release_tags, "idea-branch": check_idea}
    new_sources, ranges = {}, []
    for source in config["sources"]:
        if args.only and source["id"] not in args.only:
            new_sources[source["id"]] = old_sources.get(source["id"], {})
            continue
        prev = old_sources.get(source["id"], {})
        state, found = checkers[source["kind"]](source, prev, config, args, ctx)
        if source.get("advisories"):
            seen = set(prev.get("advisories", []))
            state["advisories"] = sorted(seen | set(by_id))
            fresh = [a for a in advisories if a["id"] not in seen]
            if fresh or args.force:
                ranges.append({
                    "id": f"{source['id']}/advisories", "source": source["id"], "kind": "advisories",
                    "repo": source["advisories"], "ref": "security advisories",
                    "base_tag": source.get("base_tag"), "advisories": [a["id"] for a in advisories],
                    "new_advisories": [a["id"] for a in fresh], "commits": [], "triage": False,
                })
        new_sources[source["id"]] = state
        ranges += found
    new_state = {"version": 1, "sources": new_sources}

    report = {"checked": now.isoformat(), "wrapper_ref": args.wrapper_ref,
              "advisories": by_id, "ranges": ranges}
    write_json(os.path.join(args.out, "report.json"), report)
    write_json(os.path.join(args.out, "state.json"), new_state)
    changed = new_state != (old_state or {"version": 1, "sources": old_sources})
    triage = [r["id"] for r in ranges if r["triage"]]
    write_issues(args.out, report, advisory_source)
    write_triage(args, report)
    summary = render_summary(report, changed)
    print(summary)
    if args.dry_run:
        for name in sorted(os.listdir(os.path.join(args.out, "issues"))):
            issue = json.load(open(os.path.join(args.out, "issues", name)))
            print(f"\n{'=' * 78}\nISSUE {issue['title']}\n{'=' * 78}\n{issue['body']}")
            if issue.get("comment"):
                print(f"--- comment ---\n{issue['comment']}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"changed={'true' if changed else 'false'}\n")
            out.write(f"ranges={'true' if ranges else 'false'}\n")
            out.write(f"triage={'true' if triage else 'false'}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(summary + "\n")


def write_json(path, data):
    with open(path, "w") as out:
        json.dump(data, out, indent=1, sort_keys=True)
        out.write("\n")


# ------------------------------------------------------------------- rendering


def md(text):
    """Neutralise untrusted upstream text for Markdown tables (no mentions, HTML or pipes)."""
    text = re.sub(r"[\x00-\x1f]", " ", str(text))
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "\\|").replace("@", "@\u200b").replace("`", "'")[:200])


def commit_link(repo, sha):
    return f"[`{sha[:10]}`](https://github.com/{repo}/commit/{sha})"


def commit_table(r, commits):
    rows = ["| | Commit | Subject | Files | Our tree |", "| --- | --- | --- | --- | --- |"]
    for c in commits[:MAX_ROWS]:
        where = c.get("in_tree") and f"yes ({c['in_tree']})" or ""
        if c.get("in_fork_branches"):
            where = (where + "; " if where else "") + "on " + ", ".join(md(b) for b in c["in_fork_branches"])
        files = c.get("fv_files") or c["files"]
        shown = ", ".join(f"`{md(f)}`" for f in files[:4]) + (f" +{len(files) - 4}" if len(files) > 4 else "")
        rows.append(f"| {'new' if c.get('new') and r.get('previous') else ''} | {commit_link(r['repo'], c['sha'])} "
                    f"| {md(c['subject'])} | {shown} | {where or 'no'} |")
    if len(commits) > MAX_ROWS:
        rows.append(f"| | … | {len(commits) - MAX_ROWS} more in the workflow artifact | | |")
    return "\n".join(rows)


def advisory_table(report, ids, base_tag=None):
    if not ids:
        return "None."
    rows = ["| Advisory | Severity | Summary | Vulnerable | Patched | Mentions FV code |"
            + (" Covers our base |" if base_tag else ""),
            "| --- | --- | --- | --- | --- | --- |" + (" --- |" if base_tag else "")]
    for ident in ids:
        a = report["advisories"][ident]
        row = (f"| [{a['id']}]({a['url']}) {a['cve'] or ''} | {a['severity']} | {md(a['summary'])} "
               f"| {md(a['vulnerable'])} | {md(a['patched'])} | {md(', '.join(a['fv_mentions'][:5])) or '-'} |")
        if base_tag:
            row += f" {dict([(True, 'yes'), (False, 'no')]).get(covers(a, base_tag), 'check')} |"
        rows.append(row)
    return "\n".join(rows)


def marker(key):
    return f"<!-- upstream-watch:key={key} -->"


def render_range(report, r):
    lines = [marker(r["id"]), ""]
    if r["kind"] == "advisories":
        lines += [f"Published GitHub security advisories of [{r['repo']}](https://github.com/{r['repo']}/security/advisories). "
                  f"\"Covers our base\" compares the vulnerable range with `{r['base_tag']}`; our edk2 also carries "
                  "later cherry-picks, so `yes` means *check*, not *vulnerable*.", "",
                  advisory_table(report, r["advisories"], r["base_tag"])]
        return "\n".join(lines)
    missing = [c for c in r["commits"] if not c.get("in_tree")]
    if r["kind"] == "stable-tags":
        lines += [
            f"New tianocore release `{r['ref']}` (previous `{r['previous']}`): {r['total_commits']} commits, "
            f"{r['changed_files']} files changed upstream. {len(r['commits'])} commits touch source directories "
            f"compiled into our FV (`.github/upstream/fv-sources.json`); {len(missing)} of them are not "
            f"recognisably in our edk2 (`{r['pin'][:10]}`).",
            "", f"Compare: https://github.com/{r['repo']}/compare/{r['previous']}...{r['ref']}", "",
            "### Commits touching FV code", "", commit_table(r, r["commits"]), "",
            "### FV source files changed", "",
            "\n".join(f"- `{md(o['path'])}` ({', '.join(o['commits'][:6])})" for o in r["overlap"][:MAX_ROWS])
            or "None.",
        ]
    elif r["kind"] == "idea-branch":
        lines += [f"> **{md(r['note'])}**", "",
                  f"`{r['ref']}` moved from `{(r['previous'] or '')[:10]}` to `{r['tip'][:10]}` "
                  f"({len(r['commits'])} commits). Commit subjects and paths only.", "",
                  commit_table(r, r["commits"])]
    else:
        what = f"tag `{r['ref']}`" if r["kind"] == "release-tags" else f"branch `{r['ref']}`"
        head = f"{r['repo']} {what} at `{r['tip'][:10]}`"
        if r.get("new_branch"):
            head += " is a **new branch**"
        if r.get("forced"):
            head += f" was **force-pushed** (previously `{r['previous'][:10]}`)"
        lines += [
            f"{head}: {len(r['commits'])} commits are not ancestors of our pin `{r['pin'][:10]}` "
            f"(merge base `{r['merge_base'][:10]}`); {len(missing)} of them are not recognisably in our tree "
            "(patch-id, cherry-pick trailer or identical subject).", "",
        ]
        if r.get("submodules"):
            lines += ["Submodule changes in this release:", ""] + \
                     [f"- `{b['path']}`: `{b['from'][:10]}` → `{b['to'][:10]}`" for b in r["submodules"]] + [""]
        lines += ["### Pending commits", "", commit_table(r, r["commits"]), "",
                  f"### Overlap with our delta ({r['fork_changed_files']} files changed since the merge base)", "",
                  "\n".join(f"- `{md(o['path'])}` ({', '.join(o['commits'][:6])})" for o in r["overlap"][:MAX_ROWS])
                  or "None."]
    lines += ["", "### Matching security advisories", "", advisory_table(report, r.get("advisories", []))]
    body = "\n".join(lines)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n… truncated; the full report is in the workflow artifact."
    return body


def range_title(r):
    if r["kind"] == "advisories":
        return f"Upstream: {r['repo']} security advisories"
    if r["kind"] == "stable-tags":
        return f"Upstream: {r['repo']} {r['ref']} ({len(r['commits'])} commits touch our FV)"
    if r["kind"] == "idea-branch":
        return f"Ideas: {r['repo']} {r['ref']} moved ({len(r['commits'])} commits)"
    pending = sum(1 for c in r["commits"] if not c.get("in_tree"))
    return f"Upstream: {r['repo']} {r['ref']} ({pending} pending commits)"


def range_comment(report, r):
    if r["kind"] == "advisories":
        if not r["new_advisories"]:
            return None
        return f"{len(r['new_advisories'])} new advisories:\n\n" + advisory_table(report, r["new_advisories"])
    if not r.get("previous") or r["kind"] in ("stable-tags", "release-tags"):
        return None
    fresh = [c for c in r["commits"] if c.get("new")]
    return (f"`{r['ref']}` moved `{r['previous'][:10]}` → `{r['tip'][:10]}`"
            + (" (force-push)" if r.get("forced") else "") + f": {len(fresh)} new commits.\n\n"
            + commit_table(r, fresh))


def write_issues(out, report, advisory_source):
    directory = os.path.join(out, "issues")
    os.makedirs(directory, exist_ok=True)
    for r in report["ranges"]:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", r["id"]) + ".json"
        comment = range_comment(report, r)
        write_json(os.path.join(directory, name), {
            "key": r["id"], "title": range_title(r), "body": render_range(report, r),
            "comment": comment, "comment_key": f"{r['id']}@{r.get('tip') or ','.join(r.get('new_advisories', []))}",
            "labels": [LABEL],
        })


def write_triage(args, report):
    """Evidence for the triage agent: range metadata, patches of new commits, our delta."""
    root = os.path.join(args.out, "triage")
    os.makedirs(root, exist_ok=True)
    config = {s["id"]: s for s in json.load(open(args.config))["sources"]}
    for r in report["ranges"]:
        if not r["triage"]:
            continue
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", r["id"])
        directory = os.path.join(root, slug)
        os.makedirs(os.path.join(directory, "patches"), exist_ok=True)
        cache = os.path.join(args.cache, f"{config[r['source']]['cache']}.git")
        todo = [c for c in r["commits"] if c.get("new") and not c.get("in_tree")][:MAX_PATCHES]
        prefetch_blobs(cache, [f"upstream-{r['source']}"], [c["sha"] for c in todo])
        for c in todo:
            patch = git("format-patch", "--stdout", "-1", c["sha"], cwd=cache)
            if len(patch) > MAX_PATCH_BYTES:
                patch = patch[:MAX_PATCH_BYTES] + "\n[patch truncated]\n"
            with open(os.path.join(directory, "patches", f"{c['sha'][:12]}.patch"), "w") as f:
                f.write(patch)
        with open(os.path.join(directory, "ours.txt"), "w") as f:
            f.write(git("log", "--no-merges", "--format=%h %s", f"{r['merge_base']}..{r['pin']}", "--",
                        cwd=cache) if r.get("merge_base") else "")
        write_json(os.path.join(directory, "range.json"), {
            **{k: v for k, v in r.items() if k != "commits"},
            "commits": [{k: c.get(k) for k in ("sha", "subject", "files", "fv_files", "in_tree",
                                               "in_fork_branches", "new")} for c in r["commits"]],
            "to_classify": [c["sha"] for c in todo],
            "advisory_details": [report["advisories"][a] for a in r.get("advisories", [])],
        })


def render_summary(report, changed):
    lines = [f"## Upstream watch {report['checked']}", ""]
    if not report["ranges"]:
        lines.append("No new upstream ranges.")
    for r in report["ranges"]:
        missing = [c for c in r["commits"] if not c.get("in_tree")]
        if r["kind"] == "advisories":
            lines.append(f"- {r['repo']} advisories: {len(r['advisories'])} published, "
                         f"{len(r['new_advisories'])} new")
            continue
        line = f"- {r['repo']} `{r['ref']}`"
        if r["kind"] == "stable-tags":
            line += (f" (`{r['previous']}`..`{r['ref']}`): {r['total_commits']} commits, {len(r['commits'])} touch "
                     f"FV code, {len(missing)} not in our tree; advisories {', '.join(r['advisories']) or 'none'}")
        else:
            span = f"`{r['commits'][-1]['sha'][:8]}`..`{r['commits'][0]['sha'][:8]}`" if r["commits"] else "-"
            line += (f" {span} at `{r['tip'][:10]}`: {len(r['commits'])} commits beyond our pin "
                     f"`{(r.get('pin') or r.get('previous') or '')[:10]}`, {len(missing)} not in our tree")
            carried = sorted({b for c in r["commits"] for b in c.get("in_fork_branches", [])})
            if carried:
                full = [b for b in carried if all(b in c.get("in_fork_branches", []) for c in r["commits"])]
                line += f"; all carried by {', '.join(full)}" if full else f"; partly on {', '.join(carried)}"
            if r.get("overlap"):
                line += f"; {len(r['overlap'])} files overlap our delta"
        line += "; triage" if r["triage"] else ""
        lines.append(line)
    lines += ["", f"State changed: {changed}"]
    return "\n".join(lines)


# --------------------------------------------------------------------- publish


def api(method, path, token, body=None, ok404=False):
    request = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        if ok404 and error.code == 404:
            return None
        raise RuntimeError(f"{method} {path}: {error.code} {error.read().decode(errors='replace')[:500]}")


def cmd_publish(args):
    token = os.environ["GH_TOKEN"]
    repo = args.repo
    existing = github_get(f"https://api.github.com/repos/{repo}/issues?state=all&labels={LABEL}&per_page=100",
                          token)
    numbers = {}
    directory = os.path.join(args.out, "issues")
    for name in sorted(os.listdir(directory)):
        issue = json.load(open(os.path.join(directory, name)))
        mark = marker(issue["key"])
        found = next((i for i in existing if "pull_request" not in i and mark in (i.get("body") or "")), None)
        if found:
            update = {"title": issue["title"], "body": issue["body"]}
            if found["state"] == "closed" and issue["comment"]:
                update["state"] = "open"
            api("PATCH", f"/repos/{repo}/issues/{found['number']}", token, update)
            number = found["number"]
            if issue["comment"]:
                comment_mark = f"<!-- upstream-watch:comment={issue['comment_key']} -->"
                comments = github_get(f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100",
                                      token)
                if not any(comment_mark in (c.get("body") or "") for c in comments):
                    api("POST", f"/repos/{repo}/issues/{number}/comments", token,
                        {"body": f"{comment_mark}\n{issue['comment']}"})
        else:
            number = api("POST", f"/repos/{repo}/issues", token,
                         {"title": issue["title"], "body": issue["body"], "labels": issue["labels"]})["number"]
        numbers[issue["key"]] = number
        print(f"{issue['key']}: #{number}")
    write_json(os.path.join(args.out, "issues.json"), numbers)


def cmd_commit_state(args):
    token = os.environ["GH_TOKEN"]
    repo, branch = args.repo, args.branch
    current = api("GET", f"/repos/{repo}/git/ref/heads/{branch}", token, ok404=True)
    current = current["object"]["sha"] if current else ""
    if current != args.parent:
        sys.exit(f"{branch} moved from '{args.parent}' to '{current}' since the check; not overwriting")
    content = open(os.path.join(args.out, "state.json")).read()
    blob = api("POST", f"/repos/{repo}/git/blobs", token,
               {"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"})
    tree = api("POST", f"/repos/{repo}/git/trees", token,
               {"tree": [{"path": "state.json", "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
    commit = api("POST", f"/repos/{repo}/git/commits", token, {
        "message": "upstream-watch: record last-seen upstream state",
        "tree": tree["sha"], "parents": [current] if current else [],
    })
    if current:
        api("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", token, {"sha": commit["sha"], "force": False})
    else:
        api("POST", f"/repos/{repo}/git/refs", token, {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    print(f"{branch}: {current or '(new)'} -> {commit['sha']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--config", default=CONFIG)
    check.add_argument("--fv-sources", default=FV_SOURCES)
    check.add_argument("--wrapper", required=True, help="checkout of this repository")
    check.add_argument("--wrapper-ref", default="HEAD", help="revision whose submodule pins are our tree")
    check.add_argument("--state", help="last-seen state (missing or empty: bootstrap from the config seeds)")
    check.add_argument("--cache", required=True, help="directory for the blob-less upstream caches")
    check.add_argument("--out", required=True)
    check.add_argument("--now", help="ISO time (tests)")
    check.add_argument("--only", action="append", help="check only this source id")
    check.add_argument("--dry-run", action="store_true", help="also print every issue body")
    check.add_argument("--local-forks", action="store_true",
                       help="also treat branches of the local submodule checkouts as fork branches")
    check.add_argument("--force", action="store_true", help="report current ranges even if already seen")
    publish = sub.add_parser("publish")
    publish.add_argument("--out", required=True)
    publish.add_argument("--repo", required=True)
    state = sub.add_parser("commit-state")
    state.add_argument("--out", required=True)
    state.add_argument("--repo", required=True)
    state.add_argument("--branch", required=True)
    state.add_argument("--parent", default="")
    args = parser.parse_args()
    if args.command == "check":
        if args.state and not os.path.exists(args.state):
            args.state = None
        cmd_check(args)
    elif args.command == "publish":
        cmd_publish(args)
    else:
        cmd_commit_state(args)


if __name__ == "__main__":
    main()
