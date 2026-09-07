"""
Shared pytest fixtures — in-memory SQLite DB for fast, isolated tests.
"""
import os
import pytest

# Force in-memory DB for all tests
os.environ["DB_PATH"] = ":memory:"
os.environ["GEMINI_API_KEY"]   = "test-key"
os.environ["TELEGRAM_TOKEN"]    = "test-token"

@pytest.fixture(autouse=True)
def fresh_db():
    """Create a fresh schema before each test."""
    from db.connection import get_conn, close_conn
    # Close any existing connection to get a fresh :memory: one
    close_conn()
    from db.migrations import run_migrations, seed_products
    run_migrations()
    seed_products()
    yield
    close_conn()
