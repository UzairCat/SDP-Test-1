"""RAT web application: FastAPI endpoints + static dashboard."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import STATIC_DIR, ensure_dirs
from .db import get_conn
from .ingestion import clone_repo, delete_repo, ingest_zip
from .metrics import author_maps, compute_metrics

app = FastAPI(title="RAT - Repo Analysis Tool")


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    get_conn()  # initialise schema


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------

class CloneRequest(BaseModel):
    url: str
    name: str | None = None


class MetricsRequest(BaseModel):
    authors: list[int] | None = None
    path: str = ""
    mode: str = "all"          # all | range | manual
    ts_from: int | None = None
    ts_to: int | None = None
    hashes: list[str] | None = None


class MergeRequest(BaseModel):
    source_ids: list[int]
    target_id: int | None = None
    target_name: str | None = None
    target_email: str | None = None


class UnmergeRequest(BaseModel):
    author_id: int


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _get_repo(repo_id: int) -> dict:
    row = get_conn().execute(
        "SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Repository not found")
    return dict(row)


def _require_ready(repo: dict) -> None:
    if repo["status"] == "error":
        raise HTTPException(409, f"Repository failed to ingest: {repo['detail']}")
    if repo["status"] != "ready":
        raise HTTPException(409, f"Repository is {repo['status']}: {repo['detail']}")


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------
# repositories
# --------------------------------------------------------------------------

@app.get("/api/repos")
def list_repos() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT id, name, status, detail, head, commit_count, author_count, created_at "
        "FROM repositories ORDER BY name")]


@app.post("/api/repos/clone")
def api_clone(req: CloneRequest) -> dict:
    try:
        repo_id = clone_repo(req.url.strip(), req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": repo_id}


@app.post("/api/repos/upload")
async def api_upload(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Please upload a .zip archive of the repository")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        repo_id = ingest_zip(tmp_path, file.filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise HTTPException(400, f"Could not read archive: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"id": repo_id}


@app.delete("/api/repos/{repo_id}")
def api_delete_repo(repo_id: int) -> dict:
    try:
        delete_repo(repo_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.get("/api/repos/{repo_id}/info")
def api_repo_info(repo_id: int) -> dict:
    repo = _get_repo(repo_id)
    _require_ready(repo)
    conn = get_conn()
    rng = conn.execute(
        "SELECT MIN(committer_ts) AS lo, MAX(committer_ts) AS hi "
        "FROM commits WHERE repo_id=?", (repo_id,)).fetchone()
    path_count = conn.execute(
        "SELECT COUNT(DISTINCT ch.path) AS n FROM changes ch "
        "JOIN commits c ON c.id=ch.commit_id WHERE c.repo_id=?",
        (repo_id,)).fetchone()["n"]
    return {
        "id": repo_id, "name": repo["name"], "head": repo["head"],
        "commit_count": repo["commit_count"], "author_count": repo["author_count"],
        "path_count": path_count,
        "first_commit_ts": rng["lo"], "last_commit_ts": rng["hi"],
    }


# --------------------------------------------------------------------------
# authors
# --------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/authors")
def api_authors(repo_id: int) -> list[dict]:
    _require_ready(_get_repo(repo_id))
    raw_to_eff, rows = author_maps(repo_id)
    commit_counts: dict[int, int] = {}
    for r in get_conn().execute(
            "SELECT author_id, COUNT(*) AS n FROM commits WHERE repo_id=? GROUP BY author_id",
            (repo_id,)):
        eff = raw_to_eff.get(r["author_id"], r["author_id"])
        commit_counts[eff] = commit_counts.get(eff, 0) + r["n"]

    groups: dict[int, list[int]] = {}
    for rid, eff in raw_to_eff.items():
        groups.setdefault(eff, []).append(rid)

    payload = []
    for eff, members in groups.items():
        row = rows.get(eff, {"name": "?", "email": "?"})
        payload.append({
            "id": eff, "name": row["name"], "email": row["email"],
            "commits": commit_counts.get(eff, 0),
            "aliases": [
                {"id": rid, "name": rows[rid]["name"], "email": rows[rid]["email"]}
                for rid in sorted(members) if rid != eff
            ],
            "members": [
                {"id": rid, "name": rows[rid]["name"], "email": rows[rid]["email"]}
                for rid in sorted(members)
            ],
        })
    payload.sort(key=lambda g: (-g["commits"], g["name"].lower()))
    return payload


@app.post("/api/repos/{repo_id}/authors/merge")
def api_merge_authors(repo_id: int, req: MergeRequest) -> dict:
    _require_ready(_get_repo(repo_id))
    conn = get_conn()
    raw_to_eff, _rows = author_maps(repo_id)

    sources = [s for s in req.source_ids if s in raw_to_eff]
    if not sources:
        raise HTTPException(400, "No valid source authors to merge")

    if req.target_id is not None:
        if req.target_id not in raw_to_eff:
            raise HTTPException(400, "Target author not found in this repository")
        target = raw_to_eff[req.target_id]
    elif req.target_name and req.target_email is not None:
        name = req.target_name.strip()
        email = req.target_email.strip()
        cur = conn.execute(
            "INSERT OR IGNORE INTO authors(repo_id, name, email) VALUES(?,?,?)",
            (repo_id, name, email))
        conn.commit()
        if cur.lastrowid:
            target = cur.lastrowid
        else:
            row = conn.execute(
                "SELECT id FROM authors WHERE repo_id=? AND name=? AND email=?",
                (repo_id, name, email)).fetchone()
            if row is None:
                raise HTTPException(500, "Failed to create target author")
            target = row["id"]
    else:
        raise HTTPException(400, "Provide either target_id or target_name + target_email")

    if target in sources:
        raise HTTPException(400, "Cannot merge an author into itself")

    for src in sources:
        if src == target:
            continue
        conn.execute("UPDATE authors SET canonical_id=? WHERE id=?", (target, src))
    conn.commit()
    return {"ok": True, "target": target, "merged": len(sources)}


@app.post("/api/repos/{repo_id}/authors/unmerge")
def api_unmerge_author(repo_id: int, req: UnmergeRequest) -> dict:
    _require_ready(_get_repo(repo_id))
    conn = get_conn()
    row = conn.execute(
        "SELECT id, canonical_id FROM authors WHERE id=? AND repo_id=?",
        (req.author_id, repo_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Author not found in this repository")
    if row["canonical_id"] is None:
        raise HTTPException(400, "This identity is not merged into another author")
    conn.execute("UPDATE authors SET canonical_id=NULL WHERE id=?", (req.author_id,))
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# tree / paths / commits
# --------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/tree")
def api_tree(repo_id: int) -> dict:
    repo = _get_repo(repo_id)
    _require_ready(repo)
    proc = subprocess.run(
        ["git", "-C", repo["path"], "ls-tree", "-r", "-z", "--name-only", "HEAD"],
        capture_output=True)
    if proc.returncode != 0:
        raise HTTPException(500, "Failed to read repository tree")
    paths = [p.decode("utf-8", "replace")
             for p in proc.stdout.split(b"\0") if p]
    return {"paths": paths}


@app.get("/api/repos/{repo_id}/paths")
def api_paths(repo_id: int, q: str = "") -> list[str]:
    _require_ready(_get_repo(repo_id))
    like = _escape_like(q.strip().lower()) + "%"
    rows = get_conn().execute(
        "SELECT DISTINCT ch.path FROM changes ch "
        "JOIN commits c ON c.id=ch.commit_id "
        "WHERE c.repo_id=? AND lower(ch.path) LIKE ? ESCAPE '\\' "
        "ORDER BY ch.path LIMIT 25", (repo_id, like))
    return [r["path"] for r in rows]


@app.get("/api/repos/{repo_id}/commits")
def api_commits(repo_id: int, query: str = "", path: str = "", authors: str = "",
                ts_from: int | None = None, ts_to: int | None = None,
                offset: int = 0, limit: int = 50) -> dict:
    """List commits matching the current dashboard filters.

    query:   free text over hash / subject / author name / author email
    path:    scope filter -- commits with changes on the path or below it
    authors: comma-separated canonical author ids
    ts_from / ts_to: committer-date window [ts_from, ts_to)
    """
    _require_ready(_get_repo(repo_id))
    if limit < 1 or limit > 200:
        limit = 50
    if offset < 0:
        offset = 0
    conn = get_conn()
    raw_to_eff, author_rows = author_maps(repo_id)

    where_parts = ["c.repo_id=?"]
    args: list = [repo_id]

    q = query.strip()
    if q:
        like = "%" + _escape_like(q.lower()) + "%"
        where_parts.append(
            "(lower(c.hash) LIKE ? ESCAPE '\\' OR lower(c.subject) LIKE ? ESCAPE '\\'"
            " OR lower(a.name) LIKE ? ESCAPE '\\' OR lower(a.email) LIKE ? ESCAPE '\\')")
        args += [like, like, like, like]

    author_q = authors.strip()
    if author_q:
        try:
            wanted = {int(x) for x in author_q.split(",") if x.strip()}
        except ValueError:
            raise HTTPException(400, "Invalid author filter") from None
        raw_ids = {rid for rid, eff in raw_to_eff.items() if eff in wanted}
        if not raw_ids:
            raw_ids = {-1}
        marks = ",".join("?" * len(raw_ids))
        where_parts.append(f"c.author_id IN ({marks})")
        args += sorted(raw_ids)

    scope = path.strip().strip("/")
    if scope:
        where_parts.append(
            "(c.id IN (SELECT ch.commit_id FROM changes ch "
            "WHERE ch.path=? OR ch.path LIKE ? ESCAPE '\\'))")
        args += [scope, _escape_like(scope) + "/%"]

    if ts_from is not None:
        where_parts.append("c.committer_ts>=?")
        args.append(int(ts_from))
    if ts_to is not None:
        where_parts.append("c.committer_ts<?")
        args.append(int(ts_to))

    where = " WHERE " + " AND ".join(where_parts)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM commits c JOIN authors a ON a.id=c.author_id" + where,
        args).fetchone()["n"]
    rows = conn.execute(
        "SELECT c.hash, c.committer_ts, c.subject, c.author_id "
        "FROM commits c JOIN authors a ON a.id=c.author_id" + where +
        " ORDER BY c.committer_ts DESC, c.id DESC LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    out = []
    for r in rows:
        eff = raw_to_eff.get(r["author_id"], r["author_id"])
        arow = author_rows.get(eff, {"name": "?", "email": "?"})
        out.append({
            "hash": r["hash"], "ts": r["committer_ts"], "subject": r["subject"],
            "author_id": eff, "author": arow["name"],
        })
    return {"total": total, "rows": out}


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

@app.post("/api/repos/{repo_id}/metrics")
def api_metrics(repo_id: int, req: MetricsRequest) -> dict:
    _require_ready(_get_repo(repo_id))
    try:
        return compute_metrics(
            repo_id,
            authors=req.authors, path=req.path, mode=req.mode,
            ts_from=req.ts_from, ts_to=req.ts_to, hashes=req.hashes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------
# static dashboard (registered last so /api/* wins)
# --------------------------------------------------------------------------

ensure_dirs()
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
