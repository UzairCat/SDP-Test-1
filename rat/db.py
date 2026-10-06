"""SQLite access layer: schema, per-thread connections, small helpers."""
import sqlite3
import threading

from .config import DB_PATH, ensure_dirs

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    head TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT NOT NULL DEFAULT '',
    commit_count INTEGER NOT NULL DEFAULT 0,
    author_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS authors(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    canonical_id INTEGER,
    UNIQUE(repo_id, name, email)
);
CREATE TABLE IF NOT EXISTS commits(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    hash TEXT NOT NULL,
    author_id INTEGER NOT NULL,
    committer_ts INTEGER NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    UNIQUE(repo_id, hash)
);
CREATE TABLE IF NOT EXISTS changes(
    commit_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_commit ON changes(commit_id);
CREATE INDEX IF NOT EXISTS idx_changes_path ON changes(path);
CREATE INDEX IF NOT EXISTS idx_commits_repo_ts ON commits(repo_id, committer_ts);
CREATE INDEX IF NOT EXISTS idx_commits_repo_author ON commits(repo_id, author_id);
"""


def get_conn() -> sqlite3.Connection:
    """Return a connection local to the current thread (created lazily)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        ensure_dirs()
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn
