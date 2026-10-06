"""Central configuration: filesystem layout and constants."""
from pathlib import Path

# Project root = directory containing the rat/ package
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
REPOS_DIR = DATA_DIR / "repos"
DB_PATH = DATA_DIR / "rat.db"
STATIC_DIR = ROOT_DIR / "static"

# Git rename-detection similarity threshold required by the spec (50%)
RENAME_THRESHOLD = "50%"

# SQLite bulk-insert batch size
INSERT_BATCH = 50_000

# How often (seconds) progress callbacks may fire at most
PROGRESS_INTERVAL = 0.25


def ensure_dirs() -> None:
    """Create the data directories on first use."""
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
