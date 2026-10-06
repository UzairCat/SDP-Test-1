#!/usr/bin/env python3
"""Validate RAT's metrics against the reference CSVs in repo-references/.

Each reference CSV contains rows for one repository at a pinned ref_sha:
  repo, ref_sha, commit_set, commit_count, object_type, path, author,
  added, removed, growth, churn, modifications, modification_frequency,
  churn_rate, ownership

object_type is one of repository (path "/"), directory, file.  The `author`
column is "ALL" for aggregate rows and "Name <email>" (mailmap-canonical) for
per-author rows.

This script:
1. Ensures each repo is ingested by RAT at exactly the pinned ref
   (clones + checks out + re-indexes when necessary).
2. Recomputes every metric from RAT's raw index (changes/commits/authors
   tables) in a single pass and compares every CSV row.
3. Spot-checks a sample of objects through the live metrics API
   (validates the serving path, not just the stored data).

Usage:  python scripts/check_references.py [path-to-repo-references-dir]
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rat.gitlog import index_repository  # noqa: E402

BASE = "http://127.0.0.1:8000"
REF_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/vmuser/Desktop/Test/repo-references")


# ----------------------------------------------------------------- api helpers
def api_get(path: str):
    with urllib.request.urlopen(BASE + path) as r:
        return json.load(r)


def api_post(path: str, body: dict):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def git(repo_path: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", repo_path, *args],
                         capture_output=True, text=True, errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


# ------------------------------------------------------------ ensure ingestion
CLONE_URLS = {
    "cJSON": "https://github.com/DaveGamble/cJSON.git",
    "git": "https://github.com/git/git.git",
    "redis": "https://github.com/redis/redis.git",
}


def ensure_repo(name: str, ref_sha: str) -> int:
    """Return the RAT repo id for `name`, indexed at exactly `ref_sha`."""
    repos = {r["name"]: r for r in api_get("/api/repos")}

    if name not in repos:
        print(f"[setup] cloning {name} ...")
        repo_id = api_post("/api/repos/clone",
                           {"url": CLONE_URLS[name], "name": name})["id"]
        while True:
            time.sleep(2)
            repos = {r["name"]: r for r in api_get("/api/repos")}
            st = repos[name]["status"]
            if st == "ready":
                break
            if st == "error":
                raise SystemExit(f"[setup] clone of {name} failed: {repos[name]['detail']}")
        repo = repos[name]
    else:
        repo = repos[name]
        repo_id = repo["id"]

    path = str(ROOT / "data" / "repos" / name)
    head = git(path, "rev-parse", "HEAD")
    if head != ref_sha:
        print(f"[setup] {name}: moving HEAD {head[:12]} -> {ref_sha[:12]} and re-indexing")
        git(path, "checkout", "--quiet", ref_sha)
        index_repository(repo_id, path)
    elif repo["status"] != "ready":
        index_repository(repo_id, path)
    return repo_id


# -------------------------------------------------- recompute from raw index
def compute_all(repo_id: int) -> dict:
    """Recompute every object metric from RAT's raw tables (spec formulas)."""
    import sqlite3
    conn = sqlite3.connect(ROOT / "data" / "rat.db")
    conn.row_factory = sqlite3.Row

    # author resolution: raw id -> effective (canonical) id, and label
    arows = conn.execute(
        "SELECT id, name, email, canonical_id FROM authors WHERE repo_id=?",
        (repo_id,)).fetchall()
    by_id = {r["id"]: r for r in arows}
    eff_of = {}
    for r in arows:
        cur, seen = r["id"], {r["id"]}
        while by_id[cur]["canonical_id"] and by_id[cur]["canonical_id"] in by_id \
                and by_id[cur]["canonical_id"] not in seen:
            cur = by_id[cur]["canonical_id"]
            seen.add(cur)
        eff_of[r["id"]] = cur
    commits = conn.execute(
        "SELECT id, author_id FROM commits WHERE repo_id=?", (repo_id,)).fetchall()
    n_h = len(commits)

    per_path = defaultdict(lambda: [0, 0, 0])               # path -> [+, -, mods]
    per_pa = defaultdict(lambda: [0, 0, 0])                 # (path, eff) -> [+, -, mods]
    dirs_sum = defaultdict(lambda: [0, 0])                  # dir -> [+, -]
    dir_mods = defaultdict(int)                             # dir -> distinct commits w/ churn
    da_sum = defaultdict(lambda: [0, 0])                    # (dir, eff) -> [+, -]
    da_mods = defaultdict(int)                              # (dir, eff) -> distinct commits

    cur = conn.execute(
        "SELECT ch.path, ch.commit_id, ch.added, ch.removed "
        "FROM changes ch JOIN commits c ON c.id=ch.commit_id "
        "WHERE c.repo_id=? ORDER BY ch.commit_id", (repo_id,))
    author_of = {c["id"]: c["author_id"] for c in commits}
    prev_cid, touched, has_churn, cur_eff = None, set(), False, None

    def flush():
        nonlocal touched, has_churn, cur_eff
        if has_churn and cur_eff is not None:
            for d in touched:
                dir_mods[d] += 1
                da_mods[(d, cur_eff)] += 1
        touched, has_churn, cur_eff = set(), False, None

    for path, cid, a, r in cur:
        eff = eff_of[author_of[cid]]
        lam = a + r
        acc = per_path[path]
        acc[0] += a
        acc[1] += r
        pacc = per_pa[(path, eff)]
        pacc[0] += a
        pacc[1] += r
        if lam > 0:
            acc[2] += 1
            pacc[2] += 1
            if cid != prev_cid:
                if prev_cid is not None:
                    flush()
                prev_cid = cid
                cur_eff = eff
            has_churn = True
            parts = path.split("/")
            for i in range(len(parts)):
                d = "/".join(parts[:i])
                dirs_sum[d][0] += a
                dirs_sum[d][1] += r
                da_sum[(d, eff)][0] += a
                da_sum[(d, eff)][1] += r
                touched.add(d)
    if prev_cid is not None:
        flush()

    conn.close()
    return {
        "n_h": n_h, "per_path": per_path, "per_pa": per_pa,
        "dirs_sum": dirs_sum, "dir_mods": dir_mods,
        "da_sum": da_sum, "da_mods": da_mods,
        "eff_of_label": {f"{by_id[e]['name']} <{by_id[e]['email']}>": e
                          for e in set(eff_of.values())},
    }


# ------------------------------------------------------------------- checking
def object_values(obj_type: str, path: str, author: str, data: dict):
    """Return (added, removed, mods, n_h) for one CSV row, or None if unknown."""
    key = path.strip("/")

    if author == "ALL":
        if obj_type == "file":
            acc = data["per_path"].get(key)
            if acc is None:
                return None
            return acc[0], acc[1], acc[2], data["n_h"]
        dacc = data["dirs_sum"].get(key)
        if dacc is None and key not in data["dir_mods"]:
            # a directory may exist with only zero-churn activity (pure renames)
            pref = key + "/"
            if not any(p.startswith(pref) for p in data["per_path"]):
                return None
            dacc = [0, 0]
        else:
            dacc = data["dirs_sum"].get(key, [0, 0])
        return dacc[0], dacc[1], data["dir_mods"].get(key, 0), data["n_h"]

    # per-author row
    eff = data["eff_of_label"].get(author)
    if eff is None:
        return None
    if obj_type == "file":
        acc = data["per_pa"].get((key, eff))
        if acc is None:
            return 0, 0, 0, data["n_h"]
        return acc[0], acc[1], acc[2], data["n_h"]
    s = data["da_sum"].get((key, eff))
    if s is None and (key, eff) not in data["da_mods"]:
        return 0, 0, 0, data["n_h"]
    s = data["da_sum"].get((key, eff), [0, 0])
    return s[0], s[1], data["da_mods"].get((key, eff), 0), data["n_h"]


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def check_csv(fname: Path, repo_id: int, api_samples: int = 8) -> bool:
    print(f"\n=== {fname.name} (repo id {repo_id}) ===")
    with open(fname) as f:
        rows = list(csv.DictReader(f))
    ref_sha = rows[0]["ref_sha"]
    if not fname.name.startswith(rows[0]["repo"]):
        raise SystemExit(f"{fname}: repo name mismatch")

    data = compute_all(repo_id)
    bad = 0
    checked = 0

    def fail(row, field, got, want):
        nonlocal bad
        bad += 1
        if bad <= 15:
            print(f"  [DIFF] {row['object_type']} {row['path'] or '/'} {row['author'][:30]}: "
                  f"{field} got={got} want={row[field]}")

    for row in rows:
        checked += 1
        vals = object_values(row["object_type"], row["path"], row["author"], data)
        if vals is None:
            fail(row, "path", "MISSING", row["path"])
            continue
        added, removed, mods, n_h = vals
        churn = added + removed
        want = row
        if int(want["commit_count"]) != n_h:
            fail(row, "commit_count", n_h, want["commit_count"])
        for field, got in (("added", added), ("removed", removed),
                           ("growth", added - removed), ("churn", churn),
                           ("modifications", mods)):
            if str(got) != want[field]:
                fail(row, field, got, want[field])
        if want["modification_frequency"]:
            eta = mods / n_h if n_h else 0.0
            if not close(eta, float(want["modification_frequency"]), 1e-9):
                fail(row, "modification_frequency", eta, want["modification_frequency"])
        if want["churn_rate"]:
            rho = churn / n_h if n_h else 0.0
            if not close(rho, float(want["churn_rate"]), 1e-9):
                fail(row, "churn_rate", rho, want["churn_rate"])
        if want["ownership"]:
            all_vals = object_values(row["object_type"], row["path"], "ALL", data)
            denom = (all_vals[0] + all_vals[1]) if all_vals else 0
            own = churn / denom if denom else 0.0
            if not close(own, float(want["ownership"]), 1e-9):
                fail(row, "ownership", own, want["ownership"])

    print(f"  bulk rows: {checked} checked, {bad} mismatches")

    # ---- spot-check a sample through the live metrics API ----
    api_bad = 0
    paths = [r for r in rows if r["author"] == "ALL"]
    step = max(1, len(paths) // api_samples)
    sample = paths[::step][:api_samples]
    sample.append(next(r for r in rows if r["object_type"] == "repository"))  # root always
    for row in sample:
        p = row["path"].strip("/")
        m = api_post(f"/api/repos/{repo_id}/metrics", {
            "authors": None, "path": p, "mode": "all",
            "ts_from": None, "ts_to": None, "hashes": None})
        t = m["totals"]
        ok = (t["added"] == int(row["added"]) and t["removed"] == int(row["removed"])
              and t["growth"] == int(row["growth"]) and t["churn"] == int(row["churn"])
              and t["mods"] == int(row["modifications"]))
        if not ok:
            api_bad += 1
            print(f"  [API-DIFF] {row['object_type']} {row['path']}: "
                  f"got added={t['added']} removed={t['removed']} churn={t['churn']} "
                  f"mods={t['mods']} want {row['added']}/{row['removed']}/"
                  f"{row['churn']}/{row['modifications']}")
    # authors payload at root scope: validates per-author rows via the API
    root = api_post(f"/api/repos/{repo_id}/metrics", {
        "authors": None, "path": "", "mode": "all",
        "ts_from": None, "ts_to": None, "hashes": None})
    api_authors = {f"{a['name']} <{a['email']}>": a for a in root["authors"]}
    ref_authors = [r for r in rows if r["object_type"] == "repository" and r["author"] != "ALL"]
    for r in ref_authors:
        a = api_authors.get(r["author"])
        if a is None:
            api_bad += 1
            print(f"  [API-DIFF] repository author missing: {r['author']}")
            continue
        ok = (a["added"] == int(r["added"]) and a["removed"] == int(r["removed"])
              and a["churn"] == int(r["churn"]) and a["mods"] == int(r["modifications"])
              and close(a["ownership"], float(r["ownership"]), 1e-4))
        if not ok:
            api_bad += 1
            print(f"  [API-DIFF] repository author {r['author']}: got "
                  f"+{a['added']}/-{a['removed']} churn={a['churn']} mods={a['mods']} "
                  f"own={a['ownership']} want {r['added']}/{r['removed']}/"
                  f"{r['churn']}/{r['modifications']}/{r['ownership']}")
    print(f"  api spot-checks: {len(sample)} objects + {len(ref_authors)} authors, "
          f"{api_bad} mismatches")
    return bad == 0 and api_bad == 0


def main() -> None:
    if not REF_DIR.is_dir():
        raise SystemExit(f"reference dir not found: {REF_DIR}")
    ok_all = True
    for fname in sorted(REF_DIR.glob("*.csv")):
        with open(fname) as f:
            first = next(csv.DictReader(f))
        name, ref_sha = first["repo"], first["ref_sha"]
        repo_id = ensure_repo(name, ref_sha)
        ok_all &= check_csv(fname, repo_id)
    print("\n" + ("ALL REFERENCE CHECKS PASSED" if ok_all else "REFERENCE CHECKS FAILED"))
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
