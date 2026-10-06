"""git log parsing and indexing into the SQLite metric store.

Correctness notes (per the COMS3011A spec):
- H-bar = non-merge commits reachable from HEAD -> `git log HEAD --no-merges`
- Rename detection at 50% similarity -> `-M50%`; renames are attributed to
  the NEW path; a pure rename contributes 0 added / 0 removed lines (it is
  still recorded so the path is visible, but lambda == 0 so it is not a
  "modification" per the spec formula).
- Binary files (numstat prints `-`) are not measured.
- Deleted files appear as `0  N  path` rows -> removals on their path.
- The initial commit diffs against an empty commit (git's default behaviour).
- Author identity = author name/email.  A `.mailmap` in the repository is
  applied via `git check-mailmap` and stored as canonical links; manual
  author merges edit the same links later.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Callable, Iterator

from .config import INSERT_BATCH, PROGRESS_INTERVAL, RENAME_THRESHOLD
from .db import get_conn

RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"

# `src/{old => new}/file.c` style rename notation
_BRACE_RENAME = re.compile(r"\{([^{}]*)\s*=>\s*([^{}]*)\}")

_C_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b",
    "f": "\f", "v": "\v", '"': '"', "\\": "\\",
}

ProgressFn = Callable[[str, float | None, str], None]


def run_git(repo_path: str, *args: str) -> str:
    """Run a git command in the repo and return stripped stdout."""
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _unquote_c(s: str) -> str:
    """Undo git's C-style path quoting (used for paths with special bytes)."""
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n in _C_ESCAPES:
                out.append(_C_ESCAPES[n])
                i += 2
                continue
            if n in "01234567":
                j = i + 1
                oct_digits = ""
                while j < len(s) and len(oct_digits) < 3 and s[j] in "01234567":
                    oct_digits += s[j]
                    j += 1
                out.append(chr(int(oct_digits, 8)))
                i = j
                continue
            if n == "x":
                j = i + 2
                hex_digits = ""
                while j < len(s) and len(hex_digits) < 2 and s[j] in "0123456789abcdefABCDEF":
                    hex_digits += s[j]
                    j += 1
                if hex_digits:
                    out.append(chr(int(hex_digits, 16)))
                    i = j
                    continue
            out.append(n)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def clean_path(raw: str) -> str | None:
    """Normalise a numstat path field, resolving rename notation to the new path."""
    p = raw
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        p = _unquote_c(p[1:-1])
    m = _BRACE_RENAME.search(p)
    if m:
        p = p[: m.start()] + m.group(2) + p[m.end():]
    elif " => " in p:
        p = p.rsplit(" => ", 1)[1]
    p = p.replace("//", "/").lstrip("/")
    if p in ("", "."):
        return None
    return p


def count_commits(repo_path: str) -> int:
    out = run_git(repo_path, "rev-list", "--count", "--no-merges", "HEAD")
    return int(out)


def stream_log(repo_path: str) -> Iterator[dict]:
    """Stream `git log --numstat` yielding one dict per commit.

    Fields: hash, name, email, ts (committer unix ts), subject,
    changes: list[(path, added, removed)] with binary files skipped and
    rename paths resolved to their new location.
    """
    fmt = (
        f"{RECORD_SEP}%H{FIELD_SEP}%an{FIELD_SEP}%ae"
        f"{FIELD_SEP}%ct{FIELD_SEP}%s"
    )
    cmd = [
        "git", "-C", repo_path,
        "-c", "core.quotePath=false",
        "log", "HEAD", "--no-merges",
        f"-M{RENAME_THRESHOLD}",
        "--numstat",
        f"--format={fmt}",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", bufsize=1 << 20,
    )
    current: dict | None = None
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith(RECORD_SEP):
            if current is not None:
                yield current
            meta = line[1:].rstrip("\n").split(FIELD_SEP, 4)
            while len(meta) < 5:
                meta.append("")
            current = {
                "hash": meta[0], "name": meta[1], "email": meta[2],
                "ts": int(meta[3]), "subject": meta[4],
                "changes": [],
            }
        elif current is not None and "\t" in line:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, raw_path = parts
            if added == "-" or removed == "-":
                continue  # binary file: not measured
            try:
                a, r = int(added), int(removed)
            except ValueError:
                continue
            path = clean_path(raw_path)
            if path:
                current["changes"].append((path, a, r))
    if current is not None:
        yield current
    proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"git log failed: {stderr.strip()}")


def mailmap_map(repo_path: str, identities: list[tuple[str, str]]) -> dict:
    """Canonicalise identities through the repo's .mailmap (if any).

    Returns {(raw_name, raw_email): (canon_name, canon_email)}.  Identities
    absent from the mailmap map to themselves.
    """
    if not identities:
        return {}
    payload = "".join(f"{n} <{e}>\n" for n, e in identities)
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, "check-mailmap", "--stdin"],
            input=payload, capture_output=True, text=True, errors="replace",
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    result: dict = {}
    lines = proc.stdout.splitlines()
    if len(lines) != len(identities):
        return {}
    for line, key in zip(lines, identities):
        m = re.match(r"^(.*)<([^<>]*)>\s*$", line)
        if m:
            result[key] = (m.group(1).strip(), m.group(2).strip())
    return result


def _set_status(repo_id: int, status: str, detail: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE repositories SET status=?, detail=? WHERE id=?",
        (status, detail, repo_id),
    )
    conn.commit()


def index_repository(repo_id: int, repo_path: str, progress: ProgressFn | None = None) -> dict:
    """Parse the repository at repo_path and store all metrics. Blocking."""
    conn = get_conn()
    progress = progress or (lambda *_: None)

    progress("indexing", 0.0, "Counting commits")
    total = count_commits(repo_path)
    head = run_git(repo_path, "rev-parse", "HEAD")

    # Clear any previous index for this repository
    conn.execute(
        "DELETE FROM changes WHERE commit_id IN "
        "(SELECT id FROM commits WHERE repo_id=?)", (repo_id,))
    conn.execute("DELETE FROM commits WHERE repo_id=?", (repo_id,))
    conn.execute("DELETE FROM authors WHERE repo_id=?", (repo_id,))
    conn.commit()

    raw_authors: dict[tuple[str, str], None] = {}
    commits: list[tuple[str, tuple[str, str], int, str]] = []
    changes: list[tuple[int, str, int, int]] = []  # (commit index, path, +, -)

    progress("parsing", 0.0, f"Parsing history (0 / {total} commits)")
    last_tick = 0.0
    for idx, commit in enumerate(stream_log(repo_path)):
        key = (commit["name"], commit["email"])
        raw_authors[key] = None
        commits.append((commit["hash"], key, commit["ts"], commit["subject"]))
        base = len(commits) - 1
        for path, a, r in commit["changes"]:
            changes.append((base, path, a, r))
        now = time.monotonic()
        if now - last_tick > PROGRESS_INTERVAL:
            last_tick = now
            pct = (idx + 1) / total if total else 1.0
            progress("parsing", pct * 0.6, f"Parsing history ({idx + 1} / {total} commits)")

    # --- authors (raw identities + mailmap canonical links) ------------------
    progress("authors", 0.7, "Applying mailmap")
    canon = mailmap_map(repo_path, list(raw_authors.keys()))

    author_ids: dict[tuple[str, str], int] = {}
    for key in raw_authors:
        cur = conn.execute(
            "INSERT INTO authors(repo_id, name, email) VALUES(?,?,?)",
            (repo_id, key[0], key[1]))
        author_ids[key] = cur.lastrowid
    for key, mapped in canon.items():
        if mapped == key:
            continue
        # ensure the canonical identity exists as a row (it may carry no
        # commits of its own)
        if mapped not in author_ids:
            cur = conn.execute(
                "INSERT INTO authors(repo_id, name, email) VALUES(?,?,?)",
                (repo_id, mapped[0], mapped[1]))
            author_ids[mapped] = cur.lastrowid
        conn.execute(
            "UPDATE authors SET canonical_id=? WHERE id=?",
            (author_ids[mapped], author_ids[key]))
    conn.commit()

    # --- commits --------------------------------------------------------------
    progress("commits", 0.8, f"Storing {len(commits)} commits")
    commit_rows = [
        (repo_id, h, author_ids[key], ts, subject)
        for h, key, ts, subject in commits
    ]
    for i in range(0, len(commit_rows), INSERT_BATCH):
        conn.executemany(
            "INSERT INTO commits(repo_id, hash, author_id, committer_ts, subject) "
            "VALUES(?,?,?,?,?)",
            commit_rows[i:i + INSERT_BATCH])
    conn.commit()
    hash_to_id = {
        row["hash"]: row["id"]
        for row in conn.execute("SELECT id, hash FROM commits WHERE repo_id=?", (repo_id,))
    }

    # --- changes ---------------------------------------------------------------
    progress("changes", 0.9, f"Storing {len(changes)} file changes")
    change_rows = [
        (hash_to_id[commits[base][0]], path, a, r)
        for base, path, a, r in changes
    ]
    for i in range(0, len(change_rows), INSERT_BATCH):
        conn.executemany(
            "INSERT INTO changes(commit_id, path, added, removed) VALUES(?,?,?,?)",
            change_rows[i:i + INSERT_BATCH])
    conn.commit()

    # --- finalise ---------------------------------------------------------------
    eff: set[int] = set()
    for row in conn.execute(
            "SELECT id, canonical_id FROM authors WHERE repo_id=?", (repo_id,)):
        eff.add(row["canonical_id"] or row["id"])
    conn.execute(
        "UPDATE repositories SET head=?, commit_count=?, author_count=?, "
        "status='ready', detail='' WHERE id=?",
        (head, len(commits), len(eff), repo_id))
    conn.commit()
    progress("done", 1.0, "Ready")
    return {"commits": len(commits), "changes": len(changes), "authors": len(eff)}
