#!/usr/bin/env python3
"""Performance benchmark: time every read API on a large repo (git.git, ~61k non-merge commits).

Usage: python3 scripts/bench_perf.py [repo_id]
Prints a table of operation -> ms. Compares against naive raw-git baselines.
"""
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
REPO = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def timed(label, fn, repeat=1):
    best = None
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
    print(f"{label:<46} {best:>9.1f} ms   ({repeat}x, best)")
    return out


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.load(r)


def main():
    repos = get("/api/repos")
    repo = next(r for r in repos if r["id"] == REPO)
    print(f"Benchmarking repo {REPO} ({repo['name']}): "
          f"{repo['commit_count']:,} non-merge commits, {repo['author_count']:,} authors\n")

    # --- API timings ---
    info = timed("GET  /info", lambda: get(f"/api/repos/{REPO}/info"), 3)
    timed("GET  /authors", lambda: get(f"/api/repos/{REPO}/authors"), 3)
    timed("GET  /tree", lambda: get(f"/api/repos/{REPO}/tree"), 3)
    timed("GET  /commits (page 1)", lambda: get(f"/api/repos/{REPO}/commits?limit=50"), 3)
    timed("GET  /commits (search 'fix')",
          lambda: get(f"/api/repos/{REPO}/commits?q=fix&limit=50"), 3)

    m = timed("POST /metrics  all", lambda: post(f"/api/repos/{REPO}/metrics", {
        "authors": None, "path": "", "mode": "all",
        "ts_from": None, "ts_to": None, "hashes": None}), 3)
    print(f"{'    -> totals':<46} commits={m['commit_count']:,} "
          f"added={m['totals']['added']:,} removed={m['totals']['removed']:,} "
          f"mods={m['totals']['mods']:,}")

    ts_from = info["last_commit_ts"] - 365 * 86400
    timed("POST /metrics  date range (last 12 mo)", lambda: post(
        f"/api/repos/{REPO}/metrics",
        {"authors": None, "path": "", "mode": "range",
         "ts_from": ts_from, "ts_to": None, "hashes": None}), 3)

    authors = get(f"/api/repos/{REPO}/authors")[:5]
    ids = [a["id"] for a in authors]
    timed("POST /metrics  5-author filter", lambda: post(
        f"/api/repos/{REPO}/metrics",
        {"authors": ids, "path": "", "mode": "all",
         "ts_from": None, "ts_to": None, "hashes": None}), 3)

    timed("POST /metrics  dir scope (t/)", lambda: post(
        f"/api/repos/{REPO}/metrics",
        {"authors": None, "path": "t", "mode": "all",
         "ts_from": None, "ts_to": None, "hashes": None}), 3)

    timed("POST /metrics  file scope (git.c)", lambda: post(
        f"/api/repos/{REPO}/metrics",
        {"authors": None, "path": "git.c", "mode": "all",
         "ts_from": None, "ts_to": None, "hashes": None}), 3)

    hashes = [c[0] for c in sqlite3.connect("data/rat.db").execute(
        f"SELECT hash FROM commits WHERE repo_id={REPO} ORDER BY committer_ts DESC LIMIT 500")]
    timed("POST /metrics  manual set (500 hashes)", lambda: post(
        f"/api/repos/{REPO}/metrics",
        {"authors": None, "path": "", "mode": "manual",
         "ts_from": None, "ts_to": None, "hashes": hashes}), 3)

    # --- raw-git baselines (the "naive tool" approach) ---
    print("\nRaw git baselines (single pass over full history):")
    repo_path = f"data/repos/{repo['name']}"
    cmd = ["git", "-C", repo_path, "log", "HEAD", "--no-merges", "-M50%",
           "--numstat", "--format=%H"]

    def naive_total():
        out = subprocess.run(cmd, capture_output=True, text=True)
        a = r = 0
        for line in out.stdout.splitlines():
            if "\t" not in line:
                continue
            parts = line.split("\t", 2)
            if parts[0] != "-":
                a += int(parts[0]); r += int(parts[1])
        return a, r

    def naive_count():
        out = subprocess.run(["git", "-C", repo_path, "log", "HEAD",
                              "--no-merges", "--format=%H"], capture_output=True, text=True)
        return out.stdout.count("\n")

    timed("git log --numstat full history (parse in py)", naive_total, 1)
    timed("git log --format=%H count commits", naive_count, 1)

    print("\nDone.")


if __name__ == "__main__":
    main()
