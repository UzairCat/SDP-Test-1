#!/usr/bin/env python3
"""End-to-end edge-case test with a synthetic repository.

Builds a tiny repo exercising: pure renames, modify+rename, binary files,
merge commits, empty commits, author/committer distinction, .mailmap
merging, and time filtering.  Zips it (with .git) and uploads it through
the live API, then asserts the resulting metrics.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

BASE = "http://127.0.0.1:8000"
failures = []


def check(name, got, want):
    ok = got == want
    print(f"[{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        failures.append(name)


def api(path, body=None, method=None):
    if body is None:
        req = urllib.request.Request(BASE + path, method=method or "GET")
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method=method or "POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def g(workdir, *args, env_extra=None, author=None, date=None):
    env = dict(os.environ)
    if author:
        env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = author
        # committer is deliberately different to prove we use the author
        env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"] = "Committer", "c@x"
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["git", "-C", str(workdir), *args],
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


# ------------------------------------------------------------------ build repo
tmp = Path = tempfile.mkdtemp(prefix="rat-synth-")
repo = os.path.join(tmp, "synthrepo")
os.makedirs(repo)
g(repo, "init", "-q", "-b", "main")

def commit(msg, author, date):
    g(repo, "add", "-A", author=author, date=date)
    g(repo, "commit", "-q", "--no-gpg-sign", "-m", msg, author=author, date=date)

# c1: Alice <alice@one>: a.txt (10 lines), binary data.bin, src/lib.py (5 lines)
with open(os.path.join(repo, "a.txt"), "w") as f:
    f.write("\n".join(f"line{i}" for i in range(10)) + "\n")
os.makedirs(os.path.join(repo, "src"))
with open(os.path.join(repo, "src", "lib.py"), "w") as f:
    f.write("\n".join(f"code{i}" for i in range(5)) + "\n")
with open(os.path.join(repo, "data.bin"), "wb") as f:
    f.write(bytes(range(256)))
with open(os.path.join(repo, ".mailmap"), "w") as f:
    f.write("Alice Smith <alice@canonical> <alice@one>\n"
            "Alice Smith <alice@canonical> <alice@two>\n")
commit("initial", ("Alice", "alice@one"), "2024-01-01T10:00:00 +0000")

# c2: Alice <alice@two> (different email, same person): modify a.txt +2/-1
with open(os.path.join(repo, "a.txt"), "a") as f:
    f.write("extra1\nextra2\n")
lines = open(os.path.join(repo, "a.txt")).read().splitlines()
lines[0] = "CHANGED"
open(os.path.join(repo, "a.txt"), "w").write("\n".join(lines) + "\n")
commit("modify a.txt", ("Alice", "alice@two"), "2024-02-01T10:00:00 +0000")

# c3: Bob: pure rename a.txt -> b.txt
g(repo, "mv", "a.txt", "b.txt", author=("Bob", "bob@x"), date="2024-03-01T10:00:00 +0000")
commit("pure rename", ("Bob", "bob@x"), "2024-03-01T10:00:00 +0000")

# c4: Bob: modify b.txt (+3) AND rename b.txt -> c.txt in the same commit
with open(os.path.join(repo, "b.txt"), "a") as f:
    f.write("n1\nn2\nn3\n")
g(repo, "mv", "b.txt", "c.txt", author=("Bob", "bob@x"), date="2024-04-01T10:00:00 +0000")
commit("modify + rename", ("Bob", "bob@x"), "2024-04-01T10:00:00 +0000")

# c5: side branch + merge commit (must be excluded from H-bar)
g(repo, "checkout", "-q", "-b", "side", author=("Bob", "bob@x"), date="2024-05-01T10:00:00 +0000")
with open(os.path.join(repo, "side.txt"), "w") as f:
    f.write("side\n")
commit("side work", ("Bob", "bob@x"), "2024-05-01T10:00:00 +0000")
g(repo, "checkout", "-q", "main", author=("Bob", "bob@x"), date="2024-05-02T10:00:00 +0000")
g(repo, "merge", "side", "--no-ff", "-m", "merge side",
  author=("Bob", "bob@x"), date="2024-05-02T10:00:00 +0000")

# c6: empty commit by Bob (counts in |H| but has no changes)
g(repo, "commit", "-q", "--allow-empty", "-m", "empty",
  author=("Bob", "bob@x"), date="2024-06-01T10:00:00 +0000")

# ------------------------------------------------------------------ upload zip
zip_path = os.path.join(tmp, "synthrepo.zip")
with zipfile.ZipFile(zip_path, "w") as zf:
    for root, _dirs, files in os.walk(repo):
        for fn in files:
            full = os.path.join(root, fn)
            zf.write(full, os.path.join("synthrepo", os.path.relpath(full, repo)))

with open(zip_path, "rb") as f:
    req = urllib.request.Request(
        BASE + "/api/repos/upload", data=f.read(),
        headers={"Content-Type": "application/octet-stream",
                 "Content-Disposition": 'attachment; filename="synthrepo.zip"'},
        method="POST")
    import mimetypes
    # use multipart manually
    boundary = "----ratboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="synthrepo.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + open(zip_path, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/repos/upload", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req) as r:
        res = json.load(r)

repo_id = res["id"]
import time
for _ in range(40):
    info = [r for r in api("/api/repos") if r["id"] == repo_id][0]
    if info["status"] in ("ready", "error"):
        break
    time.sleep(0.5)
if info["status"] != "ready":
    print("UPLOAD FAILED:", info)
    sys.exit(1)
print("uploaded synth repo as id", repo_id)

M = lambda **kw: api(f"/api/repos/{repo_id}/metrics", kw)

# ------------------------------------------------------------------ assertions
# H-bar = non-merge commits reachable from HEAD:
# initial, modify a.txt, pure rename, modify+rename, side work, empty = 6
m = M()
check("commit_count (merge excluded, empty included)", m["commit_count"], 6)
# c1: a.txt+10, .mailmap+2, src/lib.py+5 (binary skipped) = 17
# c2: a.txt +3/-1 (two appends + replaced line)
# c4: c.txt +3 (change attributed to new path)
# side: side.txt +1
check("repo added (17+3+3+1)", m["totals"]["added"], 24)
check("repo removed (1)", m["totals"]["removed"], 1)
check("repo churn", m["totals"]["churn"], 25)
# modifications: commits with churn = c1, c2, c4, side (rename+empty have none)
check("repo modifications", m["totals"]["mods"], 4)

# a.txt: +10 (c1) +3/-1 (c2), mods 2 (deleted via rename, not counted as removal)
m = M(path="a.txt")
check("a.txt added", m["totals"]["added"], 13)
check("a.txt removed", m["totals"]["removed"], 1)
check("a.txt mods", m["totals"]["mods"], 2)

# b.txt: only the pure rename row -> zero everything
m = M(path="b.txt")
check("b.txt added (pure rename only)", m["totals"]["added"], 0)
check("b.txt removed", m["totals"]["removed"], 0)
check("b.txt mods (lambda==0 is not a modification)", m["totals"]["mods"], 0)

# c.txt: modify+rename attributed to new path: +3/-0, 1 mod
m = M(path="c.txt")
check("c.txt added (change attributed to new path)", m["totals"]["added"], 3)
check("c.txt removed", m["totals"]["removed"], 0)
check("c.txt mods", m["totals"]["mods"], 1)

# data.bin: binary, not measured
m = M(path="data.bin")
check("binary file not measured", m["totals"]["churn"], 0)
check("binary file no mods", m["totals"]["mods"], 0)

# src/lib.py
m = M(path="src/lib.py")
check("src/lib.py added", m["totals"]["added"], 5)

# directory src
m = M(path="src")
check("dir src added", m["totals"]["added"], 5)
check("dir src mods", m["totals"]["mods"], 1)

# time range: from 2024-04-01 (inclusive) to 2024-06-01 (exclusive)
apr = 1711956000  # 2024-04-01T10:00:00Z
jun = 1717236000  # 2024-06-01T10:00:00Z
m = M(mode="range", ts_from=apr, ts_to=jun)
check("range |H| (modify+rename, side work)", m["commit_count"], 2)
check("range churn (+3 +1)", m["totals"]["churn"], 4)

# author metrics after mailmap: Alice Smith (canonical) has 2 commits
authors = api(f"/api/repos/{repo_id}/authors")
alice = next((a for a in authors if a["name"] == "Alice Smith"), None)
check("mailmap merged Alice into canonical identity", alice is not None, True)
check("Alice Smith commits", alice["commits"] if alice else -1, 2)
check("Alice Smith aliases (two emails merged)", len(alice["aliases"]) if alice else -1, 2)
bob = next(a for a in authors if a["name"] == "Bob")
check("Bob commits (merge commit excluded)", bob["commits"], 4)
check("committer identity not used as author",
      any(a["name"] == "Committer" for a in authors), False)

# author filter: only Alice's view (root scope: a.txt, .mailmap, src/lib.py)
m = M(authors=[alice["id"]])
check("author filter |H|", m["commit_count"], 2)
check("author filter churn (17 + 4)", m["totals"]["churn"], 21)

# manual merge: merge Bob into Alice manually, then check combined commits
check("manual merge", api(f"/api/repos/{repo_id}/authors/merge",
                          {"source_ids": [bob["id"]], "target_id": alice["id"]})["ok"], True)
m = M(authors=[alice["id"]])
check("after manual merge |H| (2+4)", m["commit_count"], 6)
check("after manual merge churn (21+4)", m["totals"]["churn"], 25)
# unmerge restores
check("unmerge", api(f"/api/repos/{repo_id}/authors/unmerge",
                     {"author_id": bob["id"]})["ok"], True)
m = M(authors=[bob["id"]])
check("after unmerge Bob commits", m["commit_count"], 4)

# cleanup test repo from RAT
api(f"/api/repos/{repo_id}", method="DELETE")
shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL SYNTHETIC CHECKS PASSED")
