"""Repository ingestion: deep clone from a URL or extract a zip archive,
then index the repository in a background thread.

Progress is reported through the `repositories.status` / `.detail` columns so
the frontend can poll a single endpoint while work happens.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from .config import PROGRESS_INTERVAL, REPOS_DIR
from .db import get_conn
from .gitlog import index_repository

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PCT_RE = re.compile(r"(\d+)%")


def _slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", name).strip("-.")
    return slug or "repo"


def _unique_name(base: str) -> str:
    conn = get_conn()
    taken = {r["name"] for r in conn.execute("SELECT name FROM repositories")}
    name, i = base, 2
    while name in taken or (REPOS_DIR / name).exists():
        name = f"{base}-{i}"
        i += 1
    return name


def name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1] or "repo"
    return _slug(tail[:-4] if tail.endswith(".git") else tail)


def _set_status(repo_id: int, status: str, detail: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE repositories SET status=?, detail=? WHERE id=?",
        (status, detail, repo_id))
    conn.commit()


def _spawn_index(repo_id: int, repo_path: str) -> None:
    def run() -> None:
        try:
            _set_status(repo_id, "indexing", "Preparing to index")
            last = {"t": 0.0}

            def progress(stage: str, pct: float | None, detail: str) -> None:
                if stage == "done":
                    return  # final status is written by index_repository itself
                now = time.monotonic()
                if now - last["t"] > PROGRESS_INTERVAL:
                    last["t"] = now
                    pct_str = f"{int(pct * 100)}%" if pct is not None else ""
                    _set_status(repo_id, "indexing", f"{pct_str} {detail}".strip())

            index_repository(repo_id, repo_path, progress)
        except Exception as exc:  # noqa: BLE001 - surface to the user via status
            _set_status(repo_id, "error", str(exc)[:500])

    threading.Thread(target=run, daemon=True).start()


def clone_repo(url: str, display_name: str | None = None) -> int:
    """Register a new repository and clone it in the background."""
    if not re.match(r"^(https?://|git@|ssh://git@)[^\s]+$", url):
        raise ValueError("URL must be an http(s):// or git@/ssh://git@ remote")

    base = _slug(display_name) if display_name else name_from_url(url)
    name = _unique_name(base)
    target = REPOS_DIR / name

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO repositories(name, path, status, detail) VALUES(?,?,?,?)",
        (name, str(target), "cloning", "Starting clone"))
    conn.commit()
    repo_id = cur.lastrowid

    def run() -> None:
        try:
            proc = subprocess.Popen(
                ["git", "clone", "--progress", url, str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            assert proc.stderr is not None
            buf = b""
            last_t = 0.0
            while True:
                ch = proc.stderr.read(1)
                if not ch:
                    break
                buf += ch
                if ch in (b"\r", b"\n"):
                    line = buf.decode("utf-8", "replace").strip()
                    buf = b""
                    if line:
                        now = time.monotonic()
                        if now - last_t > PROGRESS_INTERVAL:
                            last_t = now
                            m = _PCT_RE.search(line)
                            if m:
                                _set_status(repo_id, "cloning", f"Cloning: {line}")
            proc.stderr.close()
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"git clone failed (exit {rc})")
            _set_status(repo_id, "cloning", "Clone complete")
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(target, ignore_errors=True)
            _set_status(repo_id, "error", str(exc)[:500])
            return
        _spawn_index(repo_id, str(target))

    threading.Thread(target=run, daemon=True).start()
    return repo_id


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip while refusing path traversal (zip-slip) members."""
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise ValueError(f"Unsafe path in archive: {member.filename}")
    zf.extractall(dest)


def _find_repo_root(extract_dir: Path) -> Path:
    """Locate the directory that holds `.git` (shallowest match wins)."""
    candidates: list[tuple[int, Path]] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > 3:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        if any(e.name == ".git" for e in entries):
            candidates.append((depth, directory))
            return  # do not descend into a repository
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                walk(entry, depth + 1)

    walk(extract_dir, 0)
    if not candidates:
        raise ValueError("No .git file or directory found in the uploaded archive")
    candidates.sort(key=lambda pair: pair[0])
    depth, chosen = candidates[0]
    same_depth = [p for d, p in candidates if d == depth]
    if len(same_depth) > 1:
        raise ValueError(
            "Multiple repositories found in archive: "
            + ", ".join(p.name for p in same_depth))
    return chosen


def ingest_zip(upload_path: Path, filename: str, display_name: str | None = None) -> int:
    """Extract an uploaded zip (containing a .git), register and index it."""
    import tempfile

    if not zipfile.is_zipfile(upload_path):
        raise ValueError("Uploaded file is not a valid zip archive")

    base = _slug(display_name) if display_name else _slug(Path(filename).stem)
    name = _unique_name(base)

    with tempfile.TemporaryDirectory(prefix="rat-zip-") as tmp:
        extract_dir = Path(tmp)
        with zipfile.ZipFile(upload_path) as zf:
            _safe_extract(zf, extract_dir)
        root = _find_repo_root(extract_dir)
        # Validate the repository is usable before accepting it
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise ValueError(
                "Archive contains a .git entry but git cannot read it "
                "(broken worktree pointer or missing objects)")

        target = REPOS_DIR / name
        shutil.move(str(root), str(target))

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO repositories(name, path, status, detail) VALUES(?,?,?,?)",
        (name, str(target), "indexing", "Preparing to index"))
    conn.commit()
    repo_id = cur.lastrowid
    _spawn_index(repo_id, str(target))
    return repo_id


def delete_repo(repo_id: int) -> None:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, path FROM repositories WHERE id=?", (repo_id,)).fetchone()
    if row is None:
        raise LookupError("Repository not found")
    # Refuse to delete while a background job may be using the directory
    status = conn.execute(
        "SELECT status FROM repositories WHERE id=?", (repo_id,)).fetchone()
    if status and status["status"] in ("cloning", "indexing"):
        raise RuntimeError("Repository is busy (cloning or indexing); wait for it to finish")
    conn.execute(
        "DELETE FROM changes WHERE commit_id IN "
        "(SELECT id FROM commits WHERE repo_id=?)", (repo_id,))
    conn.execute("DELETE FROM commits WHERE repo_id=?", (repo_id,))
    conn.execute("DELETE FROM authors WHERE repo_id=?", (repo_id,))
    conn.execute("DELETE FROM repositories WHERE id=?", (repo_id,))
    conn.commit()
    path = Path(row["path"])
    if path.is_dir() and REPOS_DIR in path.parents:
        shutil.rmtree(path, ignore_errors=True)


def repo_dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total
