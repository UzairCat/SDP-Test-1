# RAT — Repo Analysis Tool

A web dashboard that makes git repositories transparent: who has impact where, which
files are hotspots, and how the project evolved over time. RAT ingests repositories
(deep clone from a URL or a zip archive containing `.git`), indexes their full
history into SQLite, and serves metric dashboards that can be filtered by
repository, author, file/directory, and arbitrary commit sets.

![tabs](https://img.shields.io/badge/tabs-Overview%20%7C%20Explorer%20%7C%20Authors%20%7C%20Commits-4f9cf9)

## Quick start

Requirements: **Python 3.10+** and **git** (2.20+ recommended, for `check-mailmap`).

```bash
pip install -r requirements.txt
python run.py
```

Then open **http://127.0.0.1:8000** in your browser.

Everything runs locally: the server, the SQLite database (`data/rat.db`) and the
cloned repositories (`data/repos/`) stay on your machine. Press `Ctrl+C` to stop.

### Adding a repository

Click **+** in the sidebar and either:

- **Clone URL** — enter an `https://` / `git@` remote; RAT deep-clones it (full
  history) and indexes it in the background with live progress, or
- **Upload zip** — upload a `.zip` of a repository that includes its `.git`
  file or directory (the archive may contain the repo at the top level or nested).

Repositories can be removed again with the × button. Multiple repositories can be
kept side by side and switched between at any time.

## Features

| Area | What you get |
| --- | --- |
| Ingestion | Deep clone from remote URL **and** zip upload with `.git` |
| Multi-repo | Any number of repositories, switched in the sidebar |
| Filtering | By repository, author(s), file/directory scope, and commit set |
| Commit sets | All history, a time window `[from, to)`, or a manually selected list of commits |
| Author merging | `.mailmap` applied automatically at ingestion; manual merge & unmerge in the Authors tab |
| Metrics | File, directory, repository, commit-set and author metric categories (see below) |
| Visualisation | Churn timeline, directory churn treemap, top-files and author charts, ownership bars |
| QoL | Path search & lazy tree, sortable tables, CSV export, toasts, loading states, progress bars |

## Metrics

All metrics follow the COMS3011A specification. Non-merge commits reachable from
`HEAD` form the base set `H̄`; every metric is computed over a selected commit set
`H ⊆ H̄`. Binary files are not measured. Renames are detected with a 50%
similarity threshold: a pure rename does not change any metric, and a
change-plus-rename is attributed to the **new** path. Deleted files count their
removed lines on their (last) path.

**Per commit h on a file f:** added lines `l⁺`, removed lines `l⁻`,
growth `δ = l⁺ − l⁻`, churn `λ = l⁺ + l⁻`.

| Metric | Definition |
| --- | --- |
| Directory metrics | subtree sums of the file metrics (recursive definition telescopes); *modifications* count **distinct** commits touching the subtree |
| Repository metrics | directory metrics at the root |
| Commit set added/removed/growth/churn | sums of the per-commit values over `H` |
| Modifications `n` | number of commits in `H` with churn > 0 on the object |
| Modification frequency `η` | `n / |H|` |
| Churn rate `ρ` | `λ / |H|` (lines per commit) |
| Author modifications / churn | the above restricted to commits authored by the author |
| Author ownership `ω` | the author's share of an object's churn: `λ_a / λ` |

Committer dates drive time-window filters, with the spec's inclusive/exclusive
bounds (`H_i,j = {h | i ≤ date(h) < j}`). Author identities merge through
`.mailmap` (via `git check-mailmap`) and can be merged/split manually at any
time — all metrics update instantly because attribution is resolved at query
time.

## Architecture

```
run.py              entry point (uvicorn)
rat/
  app.py            FastAPI endpoints + static dashboard hosting
  ingestion.py      clone / zip extraction (background threads, progress)
  gitlog.py         `git log --numstat` streaming parser → SQLite
  metrics.py        commit-set resolution + metric aggregation engine
  db.py             SQLite schema & per-thread connections
  config.py         paths & constants
static/             single-page dashboard (vanilla JS + vendored ECharts)
scripts/            correctness test suites (see below)
data/               runtime: SQLite DB + cloned repositories (gitignored)
```

**Why it is fast on ~100 000-commit repositories**

- Ingestion streams `git log --numstat` (rename detection enabled) once and
  batch-inserts into SQLite — no per-commit git invocations.
- The metric store is minimal: one row per (commit, changed path) — the raw
  material every metric is derived from.
- Every dashboard query is a single pass: the commit set is materialised into a
  temporary table, joined against the change rows, and aggregated in one
  ordered scan (directory roll-ups and distinct-commit modification counts fall
  out of the same pass).
- Attribution filters (author merging) are resolved in-memory at query time, so
  merges/unmerges never require re-indexing.

**Measured on git.git** (61 101 non-merge commits, 136 834 change rows,
2 498 authors, 24 MB database):

| Operation | Time |
| --- | --- |
| Full ingestion (clone + index) | ~3 min (indexing alone: **28 s**) |
| Metrics — entire history (every category) | **0.43 s** |
| Metrics — date range / manual set | 0.04–0.05 s |
| Metrics — author filter / directory or file scope | 0.2–0.4 s |
| File tree, commit search, author list | ≤ 0.03 s |
| *Baseline: single naive `git log --numstat` pass* | *26 s* |

Every dashboard interaction stays well under a second, and answers the
same full-history question **~60× faster** than re-running git each time.

## Testing

Two suites run against a live server (`python run.py` first):

```bash
python scripts/verify_metrics.py   # cJSON metrics vs independently computed raw git numbers
python scripts/test_synthetic.py   # synthetic repo: renames, binary files, .mailmap,
                                   # merge commits, empty commits, zip upload, manual merges
python scripts/bench_perf.py       # performance benchmark against a large repo
```

The synthetic suite builds a tiny repository exercising every edge case in the
spec, uploads it as a zip through the live API, and asserts the exact expected
numbers for each metric category. On large repositories, totals have been
cross-checked against raw git output: git.git's full-history added/removed
(4 070 371 / 2 375 604), commit count, and mailmap-merged author counts
(e.g. Junio C Hamano, 8 420 commits over 10 identities) all match exactly.

## AI Usage Declaration

Usage:

This repository makes use of AI code generation using the following tools: Qoder[Deepseek-Flash].

This repository does not use AI in-line editing tools.

This repository does not use AI code review.

---

AI Declaration: The preceding document was generated with the assistance of the following: Qoder[Deepseek-Flash].
