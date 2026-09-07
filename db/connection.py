"""
DB connection module.
- Uses WAL journal mode for better read concurrency on SQLite.
- BEGIN IMMEDIATE on write connections to serialize concurrent writes.
- Thread-local connections for safety with python-telegram-bot's async threads.
"""
import re
import sqlite3
import threading
import os
from pathlib import Path

_local = threading.local()


def _normalize(value: str | None) -> str | None:
    """
    SQLite scalar function ``norm(text)``.

    Lowercases the string and removes punctuation variations so that minor
    spelling differences of the same brand or name compare as equal:

        norm("Haldiram's") == norm("Haldiram")   # both → "haldiram"
        norm("Amul-Lite")  == norm("Amul Lite")  # both → "amul lite"

    Normalization steps (in order):
      1. Strip possessive suffixes ('s / 's / \u2019s) so the trailing 's' is
         removed, not fused into the word.
      2. Remove remaining apostrophes, backticks, and curly quotes.
      3. Replace hyphens and dots with a space (so "Amul-Lite" splits cleanly).
      4. Collapse multiple spaces and lowercase.

    Returns None unchanged so NULL-safe SQL patterns still work.
    """
    if value is None:
        return None
    # Step 1: drop possessive 's (ASCII apostrophe, backtick, or Unicode curly quotes)
    s = re.sub(r"[''`\u2018\u2019]s\b", "", value, flags=re.IGNORECASE)
    # Step 2: remove any remaining apostrophe-like characters
    s = re.sub(r"[''`\u2018\u2019]", "", s)
    # Step 3: hyphens and full-stops → space (so "Amul-Lite" → "amul lite")
    s = re.sub(r"[-.]", " ", s)
    # Step 4: collapse whitespace and lowercase
    return re.sub(r"\s+", " ", s).strip().lower()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection, creating it if needed."""
    # Read DB_PATH dynamically so test overrides of os.environ work
    db_path_str = os.getenv("DB_PATH", "data/supermarket.db")
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = Path(db_path_str)
        if db_path_str != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path_str, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        # Register fuzzy-normalization scalar for brand/name matching.
        # Strips apostrophes, hyphens, dots and lowercases so that e.g.
        # "Haldiram" and "Haldiram's" are treated as the same string.
        conn.create_function("norm", 1, _normalize, deterministic=True)
        # WAL mode: readers don't block writers; writers don't block readers.
        conn.execute("PRAGMA journal_mode=WAL")
        # Foreign key enforcement
        conn.execute("PRAGMA foreign_keys=ON")
        # Synchronous=NORMAL is safe with WAL and fast enough for this workload
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return _local.conn


def get_conn() -> sqlite3.Connection:
    return _get_conn()


def close_conn():
    """Close the thread-local connection (call on thread shutdown)."""
    if hasattr(_local, "conn") and _local.conn:
        _local.conn.close()
        _local.conn = None
