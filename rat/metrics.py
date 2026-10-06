"""Metric computation per the COMS3011A spec.

Everything is derived from the per-commit change rows stored by the indexer:

- File metrics     l+, l-, delta = l+ - l-, lambda = l+ + l-
- Directory metrics: subtree sums of file metrics (the recursive definition
  in the spec telescopes to a subtree roll-up), with "modifications" counted
  as DISTINCT commits touching the subtree (not a sum of child counts).
- Repository metrics: directory metrics at the root.
- Commit set metrics: sums over the selected commit set H, plus
  modifications n, modification frequency eta = n/|H|, churn rate
  rho = lambda/|H|.
- Author metrics: churn / modifications attributed via the commit's
  (merged) author, and ownership omega = lambda_a / lambda.
"""
from __future__ import annotations

from .db import get_conn


def author_maps(repo_id: int) -> tuple[dict[int, int], dict[int, dict]]:
    """Load a repo's author rows.

    Returns:
      raw_to_eff: raw author id -> canonical author id (cycle-safe)
      rows: author id -> {"name", "email", "canonical_id"}
    """
    conn = get_conn()
    rows = {
        r["id"]: {"name": r["name"], "email": r["email"], "canonical_id": r["canonical_id"]}
        for r in conn.execute("SELECT id, name, email, canonical_id FROM authors WHERE repo_id=?", (repo_id,))
    }
    raw_to_eff: dict[int, int] = {}
    for aid in rows:
        seen = {aid}
        cur = aid
        while True:
            canon = rows[cur]["canonical_id"]
            if canon is None or canon not in rows or canon in seen:
                break
            cur = canon
            seen.add(cur)
        raw_to_eff[aid] = cur
    return raw_to_eff, rows


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _select_commits(repo_id: int, mode: str, ts_from: int | None, ts_to: int | None,
                    hashes: list[str] | None) -> list[tuple]:
    """Return (id, author_id, committer_ts, hash) for the requested commit set.

    Author filtering is applied afterwards in Python (canonical mapping).
    """
    conn = get_conn()
    base = "SELECT id, author_id, committer_ts, hash FROM commits WHERE repo_id=?"
    rows: list[tuple] = []
    if mode == "range":
        q, args = base, [repo_id]
        if ts_from is not None:
            q += " AND committer_ts>=?"
            args.append(int(ts_from))
        if ts_to is not None:
            q += " AND committer_ts<?"
            args.append(int(ts_to))
        rows = [tuple(r) for r in conn.execute(q, args)]
    elif mode == "manual":
        wanted = [h.lower() for h in (hashes or [])]
        seen: set[str] = set()
        for chunk in _chunks(wanted, 500):
            marks = ",".join("?" * len(chunk))
            q = base + f" AND lower(hash) IN ({marks})"
            for r in conn.execute(q, [repo_id, *chunk]):
                if r[3].lower() not in seen:
                    seen.add(r[3].lower())
                    rows.append(tuple(r))
    else:  # all
        rows = [tuple(r) for r in conn.execute(base, (repo_id,))]
    return rows


def compute_metrics(repo_id: int, *, authors: list[int] | None = None,
                    path: str = "", mode: str = "all",
                    ts_from: int | None = None, ts_to: int | None = None,
                    hashes: list[str] | None = None) -> dict:
    """Compute the full metrics payload for the given filters."""
    conn = get_conn()
    raw_to_eff, author_rows = author_maps(repo_id)

    scope = (path or "").strip().strip("/")
    prefix = scope + "/" if scope else ""

    # ---- resolve commit set H -------------------------------------------------
    if mode not in ("all", "range", "manual"):
        mode = "all"
    sel_rows = _select_commits(repo_id, mode, ts_from, ts_to, hashes)

    allowed_raw: set[int] | None = None
    if authors:
        eff_set = set(authors)
        allowed_raw = {rid for rid, eff in raw_to_eff.items() if eff in eff_set}
        if not allowed_raw:
            allowed_raw = {-1}
        sel_rows = [row for row in sel_rows if row[1] in allowed_raw]

    n_h = len(sel_rows)

    empty = {
        "repo_id": repo_id, "commit_count": n_h,
        "scope": {"path": scope, "type": "dir"},
        "totals": _totals_dict(0, 0, 0, n_h),
        "files_touched": 0,
        "children": [], "authors": [], "top_files": [], "series": [],
        "file_detail": None,
    }
    if n_h == 0:
        return empty

    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS sel(id INTEGER PRIMARY KEY, author_id INTEGER, ts INTEGER)")
    conn.execute("DELETE FROM sel")
    conn.executemany("INSERT INTO sel VALUES(?,?,?)",
                     [(r[0], r[1], r[2]) for r in sel_rows])
    # End the implicit transaction started by the DML above, otherwise this
    # connection would keep serving stale snapshots to later requests.
    conn.commit()

    # ---- one pass over all change rows of H ------------------------------------
    per_path: dict[str, list[int]] = {}          # path -> [added, removed, mods]
    per_path_author: dict[tuple[str, int], list[int]] = {}
    dirs_sum: dict[str, list[int]] = {}          # dir -> [added, removed]
    dir_mods: dict[str, int] = {}                # dir -> distinct commits with churn
    # (dir, author) -> distinct commits with churn: per the spec's I_n(h, o)
    # modifications count COMMITS, so an author's modifications on a directory
    # is the number of their commits touching the subtree -- not a sum of
    # per-file counts (one commit touching two files must count once).
    dir_author_mods: dict[tuple[str, int], int] = {}

    cur = conn.execute(
        "SELECT ch.path, ch.commit_id, ch.added, ch.removed, s.author_id "
        "FROM changes ch JOIN sel s ON s.id = ch.commit_id "
        "ORDER BY ch.commit_id")

    prev_cid: int | None = None
    touched_dirs: set[str] = set()
    commit_has_churn = False
    commit_eff: int | None = None

    def _flush_commit() -> None:
        nonlocal touched_dirs, commit_has_churn, commit_eff
        if commit_has_churn and commit_eff is not None:
            for d in touched_dirs:
                dir_mods[d] = dir_mods.get(d, 0) + 1
                key = (d, commit_eff)
                dir_author_mods[key] = dir_author_mods.get(key, 0) + 1
        touched_dirs = set()
        commit_has_churn = False
        commit_eff = None

    for p, cid, a, r, aid in cur:
        eff = raw_to_eff.get(aid, aid)
        lam = a + r
        # file sums
        acc = per_path.get(p)
        if acc is None:
            acc = per_path[p] = [0, 0, 0]
        acc[0] += a
        acc[1] += r
        # (path, author) sums
        key = (p, eff)
        pacc = per_path_author.get(key)
        if pacc is None:
            pacc = per_path_author[key] = [0, 0, 0]
        pacc[0] += a
        pacc[1] += r
        if lam > 0:
            acc[2] += 1
            pacc[2] += 1
            if cid != prev_cid:
                if prev_cid is not None:
                    _flush_commit()
                prev_cid = cid
                commit_eff = eff
            commit_has_churn = True
            # ancestor directories (root '' included)
            parts = p.split("/")
            for i in range(len(parts)):
                d = "/".join(parts[:i])
                dac = dirs_sum.get(d)
                if dac is None:
                    dac = dirs_sum[d] = [0, 0]
                dac[0] += a
                dac[1] += r
                touched_dirs.add(d)
        # pure-rename rows (lam == 0) add nothing to any metric, but the
        # path stays visible through per_path so it can be inspected.
    if prev_cid is not None:
        _flush_commit()

    # ---- commits per author (over H) -------------------------------------------
    author_commits: dict[int, int] = {}
    for aid, cnt in conn.execute("SELECT author_id, COUNT(*) FROM sel GROUP BY author_id"):
        eff = raw_to_eff.get(aid, aid)
        author_commits[eff] = author_commits.get(eff, 0) + cnt

    # ---- scope resolution --------------------------------------------------------
    scope_type = "dir"
    if scope and scope in per_path:
        scope_type = "file"
    elif scope and not any(p.startswith(prefix) for p in per_path) and scope not in dirs_sum:
        # no activity at all under this path in H -> zero metrics
        empty["scope"] = {"path": scope, "type": "unknown"}
        return empty

    if scope_type == "file":
        acc = per_path[scope]
        scope_a, scope_r, scope_m = acc[0], acc[1], acc[2]
    else:
        dacc = dirs_sum.get(scope, [0, 0])
        scope_a, scope_r = dacc[0], dacc[1]
        scope_m = dir_mods.get(scope, 0)

    totals = _totals_dict(scope_a, scope_r, scope_m, n_h)

    # ---- children of a directory scope -------------------------------------------
    children: list[dict] = []
    if scope_type == "dir":
        subdirs: set[str] = set()
        for p, acc in per_path.items():
            if prefix and not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if "/" in rest:
                subdirs.add(rest.split("/", 1)[0])
            else:
                children.append(_obj_row(rest, p, "file", acc, n_h))
        for sub in subdirs:
            sub_path = prefix + sub
            dacc = dirs_sum.get(sub_path, [0, 0])
            children.append(_obj_row(sub, sub_path, "dir",
                                     [dacc[0], dacc[1], dir_mods.get(sub_path, 0)], n_h))
        children.sort(key=lambda c: (-c["churn"], c["name"]))

    # ---- top files in scope --------------------------------------------------------
    scoped_paths = (
        [(scope, per_path[scope])] if scope_type == "file"
        else [(p, acc) for p, acc in per_path.items() if not prefix or p.startswith(prefix)]
    )
    top_files = sorted(
        ({"path": p, "name": p[len(prefix):] or p, "added": acc[0], "removed": acc[1],
          "growth": acc[0] - acc[1], "churn": acc[0] + acc[1], "mods": acc[2]}
         for p, acc in scoped_paths),
        key=lambda x: -x["churn"])[:15]

    # ---- author breakdown (within scope) ---------------------------------------------
    per_author_scope: dict[int, list[int]] = {}
    for (p, eff), acc in per_path_author.items():
        in_scope = (p == scope) if scope_type == "file" else (not prefix or p.startswith(prefix))
        if in_scope:
            a2 = per_author_scope.get(eff)
            if a2 is None:
                a2 = per_author_scope[eff] = [0, 0, 0]
            a2[0] += acc[0]
            a2[1] += acc[1]
            a2[2] += acc[2]

    scope_churn = scope_a + scope_r
    authors_payload = []
    for eff in set(list(author_commits.keys()) + list(per_author_scope.keys())):
        row = author_rows.get(eff, {"name": "?", "email": "?"})
        a2 = per_author_scope.get(eff, [0, 0, 0])
        churn_a = a2[0] + a2[1]
        # modifications = the author's COMMITS touching the scope (distinct),
        # matching the reference semantics; per-file sums only for a file scope
        mods_a = (a2[2] if scope_type == "file"
                  else dir_author_mods.get((scope, eff), 0))
        ownership = (churn_a / scope_churn) if scope_churn > 0 else 0.0
        authors_payload.append({
            "id": eff, "name": row["name"], "email": row["email"],
            "commits": author_commits.get(eff, 0),
            "added": a2[0], "removed": a2[1], "growth": a2[0] - a2[1],
            "churn": churn_a, "mods": mods_a,
            "ownership": round(ownership, 4),
        })
    if authors:  # keep only explicitly selected canonical authors
        eff_set = set(authors)
        authors_payload = [a for a in authors_payload if a["id"] in eff_set]
    authors_payload.sort(key=lambda a: (-a["churn"], a["name"]))

    # ---- time series --------------------------------------------------------------------
    if scope_type == "file":
        where, args = " WHERE ch.path=?", [scope]
    elif prefix:
        where, args = " WHERE ch.path LIKE ? ESCAPE '\\'", [_escape_like(prefix) + "%"]
    else:
        where, args = "", []
    series_rows = conn.execute(
        "SELECT strftime('%Y-%m', s.ts, 'unixepoch') AS ym, "
        "SUM(ch.added), SUM(ch.removed) "
        "FROM changes ch JOIN sel s ON s.id = ch.commit_id" + where +
        " GROUP BY ym ORDER BY ym", args).fetchall()
    commit_counts = dict(
        conn.execute(
            "SELECT strftime('%Y-%m', ts, 'unixepoch') AS ym, COUNT(*) "
            "FROM sel GROUP BY ym"))
    series = [
        {"month": ym, "added": a or 0, "removed": r or 0,
         "churn": (a or 0) + (r or 0), "commits": commit_counts.get(ym, 0)}
        for ym, a, r in series_rows
    ]

    # ---- file detail (author ownership for a single file) ---------------------------------
    file_detail = None
    if scope_type == "file":
        acc = per_path[scope]
        detail_authors = []
        for (p, eff), aacc in per_path_author.items():
            if p != scope:
                continue
            row = author_rows.get(eff, {"name": "?", "email": "?"})
            churn_a = aacc[0] + aacc[1]
            detail_authors.append({
                "id": eff, "name": row["name"], "email": row["email"],
                "added": aacc[0], "removed": aacc[1], "churn": churn_a,
                "mods": aacc[2],
                "ownership": round(churn_a / scope_churn, 4) if scope_churn else 0.0,
            })
        detail_authors.sort(key=lambda a: -a["churn"])
        file_detail = {"path": scope, "totals": totals, "authors": detail_authors}

    return {
        "repo_id": repo_id,
        "commit_count": n_h,
        "scope": {"path": scope, "type": scope_type},
        "totals": totals,
        "files_touched": len(scoped_paths),
        "children": children,
        "authors": authors_payload,
        "top_files": top_files,
        "series": series,
        "file_detail": file_detail,
    }


def _obj_row(name: str, path: str, otype: str, acc: list[int], n_h: int) -> dict:
    a, r, m = acc
    return {
        "name": name, "path": path, "type": otype,
        "added": a, "removed": r, "growth": a - r, "churn": a + r,
        "mods": m,
        "mod_freq": round(m / n_h, 4) if n_h else 0.0,
        "churn_rate": round((a + r) / n_h, 4) if n_h else 0.0,
    }


def _totals_dict(added: int, removed: int, mods: int, n_h: int) -> dict:
    churn = added + removed
    return {
        "added": added, "removed": removed,
        "growth": added - removed, "churn": churn,
        "mods": mods,
        "mod_freq": round(mods / n_h, 4) if n_h else 0.0,
        "churn_rate": round(churn / n_h, 4) if n_h else 0.0,
    }
