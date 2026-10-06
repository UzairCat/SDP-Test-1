#!/usr/bin/env python3
"""Independent correctness checks: compare RAT metrics against raw git output.

Ground truth is computed directly from `git log` with awk-style parsing that
is deliberately different from RAT's indexing code path.
"""
import json
import re
import subprocess
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
REPO = "data/repos/cJSON"

failures = []


def check(name, got, want, tol=0):
    ok = (abs(got - want) <= tol) if isinstance(got, (int, float)) else got == want
    status = "OK " if ok else "FAIL"
    print(f"[{status}] {name}: got={got} want={want}")
    if not ok:
        failures.append(name)


def api(path, body=None):
    if body is None:
        with urllib.request.urlopen(BASE + path) as r:
            return json.load(r)
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def git_numstat_totals(args):
    """Sum added/removed over numstat rows for the given git log args."""
    proc = subprocess.run(
        ["git", "-C", REPO, "-c", "core.quotePath=false", "log",
         "--no-merges", "-M50%", "--numstat", "--format=%x1e%H"] + args,
        capture_output=True, text=True, errors="replace")
    assert proc.returncode == 0, proc.stderr
    added = removed = 0
    for line in proc.stdout.splitlines():
        if not line or line.startswith("\x1e"):
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[0] == "-" or parts[1] == "-":
            continue  # binary
        path = parts[2]
        # resolve rename notation to new path
        m = re.search(r"\{([^{}]*)\s*=>\s*([^{}]*)\}", path)
        if m:
            path = path[:m.start()] + m.group(2) + path[m.end():]
        elif " => " in path:
            path = path.rsplit(" => ", 1)[1]
        added += int(parts[0])
        removed += int(parts[1])
    return added, removed


def git_path_totals(path):
    """Totals for one path, accounting for renames onto that path."""
    return git_numstat_totals(["--follow" if False else "--", path])


def git_commit_count(extra=None):
    args = ["rev-list", "--count", "--no-merges", "HEAD"]
    if extra:
        args += extra
    out = subprocess.run(["git", "-C", REPO] + args,
                         capture_output=True, text=True)
    return int(out.stdout.strip())


# ---------------------------------------------------------------- repo totals
m = api("/api/repos/1/metrics", {})
tot = m["totals"]
check("repo commit_count == |H|", m["commit_count"], git_commit_count())

# ground truth: full-history numstat sums (all paths)
g_add, g_rem = git_numstat_totals([])
check("repo added", tot["added"], g_add)
check("repo removed", tot["removed"], g_rem)
check("repo growth", tot["growth"], g_add - g_rem)
check("repo churn", tot["churn"], g_add + g_rem)

# modifications: distinct commits touching anything (lambda>0)
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges", "-M50%", "--numstat",
     "--format=%x1e%H"],
    capture_output=True, text=True, errors="replace")
mods = 0
for block in proc.stdout.split("\x1e")[1:]:
    touched = False
    for line in block.splitlines()[1:]:
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0] != "-" and parts[1] != "-":
            if int(parts[0]) + int(parts[1]) > 0:
                touched = True
    if touched:
        mods += 1
check("repo modifications (commits with churn)", tot["mods"], mods)

# ---------------------------------------------------------------- file metrics
file_path = "cJSON.c"
m = api("/api/repos/1/metrics", {"path": file_path})
ftot = m["totals"]
# ground truth: log restricted to that path; renames FROM elsewhere don't show
g_add, g_rem = git_numstat_totals(["--", file_path])
check(f"file {file_path} added", ftot["added"], g_add)
check(f"file {file_path} removed", ftot["removed"], g_rem)

# file modifications: distinct commits with lambda>0 on that path
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges", "-M50%", "--numstat",
     "--format=%x1e%H", "--", file_path],
    capture_output=True, text=True, errors="replace")
fmods = 0
for block in proc.stdout.split("\x1e")[1:]:
    for line in block.splitlines()[1:]:
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0] != "-" and parts[1] != "-":
            if int(parts[0]) + int(parts[1]) > 0:
                fmods += 1
                break
check(f"file {file_path} modifications", ftot["mods"], fmods)

# ---------------------------------------------------------------- directory metrics
d = "tests"
m = api("/api/repos/1/metrics", {"path": d})
dtot = m["totals"]
# ground truth: numstat rows filtered by the directory pathspec, keeping rows
# whose resolved (new) path is inside the subtree
g_add = g_rem = 0
for line in subprocess.run(
        ["git", "-C", REPO, "log", "--no-merges", "-M50%", "--numstat",
         "--format=%x1e%H", "--", d + "/"],
        capture_output=True, text=True, errors="replace").stdout.splitlines():
    if not line or line.startswith("\x1e"):
        continue
    parts = line.split("\t", 2)
    if len(parts) != 3 or parts[0] == "-" or parts[1] == "-":
        continue
    path = parts[2]
    mt = re.search(r"\{([^{}]*)\s*=>\s*([^{}]*)\}", path)
    if mt:
        path = path[:mt.start()] + mt.group(2) + path[mt.end():]
    elif " => " in path:
        path = path.rsplit(" => ", 1)[1]
    if path.startswith(d + "/"):
        g_add += int(parts[0])
        g_rem += int(parts[1])
check(f"dir {d}/ added", dtot["added"], g_add)
check(f"dir {d}/ removed", dtot["removed"], g_rem)

# directory mods: distinct commits touching subtree
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges", "-M50%", "--numstat",
     "--format=%x1e%H", "--", d + "/"],
    capture_output=True, text=True, errors="replace")
dmods = 0
for block in proc.stdout.split("\x1e")[1:]:
    for line in block.splitlines()[1:]:
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0] != "-" and parts[1] != "-":
            if int(parts[0]) + int(parts[1]) > 0:
                dmods += 1
                break
check(f"dir {d}/ modifications (distinct commits)", dtot["mods"], dmods)

# ---------------------------------------------------------------- time range set
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges", "--format=%ct"],
    capture_output=True, text=True)
ts_list = sorted(int(t) for t in proc.stdout.split())
mid = ts_list[len(ts_list) // 2]
# H_i,j = {h | i <= ts < j}
want_count = sum(1 for t in ts_list if mid <= t < ts_list[-1] + 1)
m = api("/api/repos/1/metrics", {"mode": "range", "ts_from": mid,
                                 "ts_to": ts_list[-1] + 1})
check("range commit set |H|", m["commit_count"], want_count)

# H_t = {h | t <= ts}
want_count = sum(1 for t in ts_list if mid <= t)
m = api("/api/repos/1/metrics", {"mode": "range", "ts_from": mid})
check("H_t commit set |H|", m["commit_count"], want_count)

# ---------------------------------------------------------------- manual commit set
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges", "--format=%H"],
    capture_output=True, text=True)
hashes = proc.stdout.split()[:100]
m = api("/api/repos/1/metrics", {"mode": "manual", "hashes": hashes})
check("manual commit set |H|", m["commit_count"], len(hashes))
# churn over that set = sum of numstat churn of exactly those commits
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges", "-M50%", "--numstat",
     "--format=%x1e%H", "--no-walk=unsorted"] + hashes[:100],
    capture_output=True, text=True, errors="replace")
want_churn = 0
for line in proc.stdout.splitlines():
    if not line or line.startswith("\x1e"):
        continue
    parts = line.split("\t", 2)
    if len(parts) == 3 and parts[0] != "-" and parts[1] != "-":
        want_churn += int(parts[0]) + int(parts[1])
check("manual set churn", m["totals"]["churn"], want_churn)

# ---------------------------------------------------------------- author metrics
authors = api("/api/repos/1/authors")
top = authors[0]
name = top["name"]
# An effective author may span several raw identities (a manual merge creates
# canonical links; cJSON has no .mailmap), so ground truth counts commits by
# exact (name, email) identity over EVERY member of the author's group.
identities = {(m["name"], m["email"]) for m in top.get("members", [top])}
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges",
     "--format=%an%x1f%ae%x1f%H"],
    capture_output=True, text=True)
author_commits = sum(
    1 for line in proc.stdout.split("\n")
    if tuple(line.split("\x1f", 2)[:2]) in identities)
# find author by name in API list
m = api("/api/repos/1/metrics", {})
api_author = next(a for a in m["authors"] if a["name"] == name)
check(f"author '{name}' commits", api_author["commits"], author_commits)

# author churn on a specific file (same merged-identity grouping)
proc = subprocess.run(
    ["git", "-C", REPO, "log", "--no-merges", "-M50%", "--numstat",
     "--format=%x1e%an%x1f%ae", "--", file_path],
    capture_output=True, text=True, errors="replace")
want_a = want_r = 0
cur_ident = None
# NOTE: str.splitlines() treats \x1e as a line boundary, so parse on "\n".
for line in proc.stdout.split("\n"):
    if line.startswith("\x1e"):
        parts = line[1:].split("\x1f", 1)
        cur_ident = tuple(parts) if len(parts) == 2 else None
        continue
    if cur_ident not in identities:
        continue
    parts = line.split("\t", 2)
    if len(parts) == 3 and parts[0] != "-" and parts[1] != "-":
        want_a += int(parts[0])
        want_r += int(parts[1])
m = api("/api/repos/1/metrics", {"path": file_path})
detail = m["file_detail"]["authors"]
api_a = next((a for a in detail if a["name"] == name), None)
if api_a:
    check(f"author '{name}' added on {file_path}", api_a["added"], want_a)
    check(f"author '{name}' removed on {file_path}", api_a["removed"], want_r)
else:
    check(f"author '{name}' present in file detail", False, True)

# ---------------------------------------------------------------- ownership sum
m = api("/api/repos/1/metrics", {})
total_ownership = round(sum(a["ownership"] for a in m["authors"]), 2)
check("ownership fractions sum to ~1", total_ownership, 1.0, tol=0.01)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
