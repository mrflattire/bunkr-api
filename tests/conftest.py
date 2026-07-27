import pytest
import sqlite3
from bunkr_api.core.db import DatabaseManager

@pytest.fixture
def temp_db(tmp_path):
    """Provides a clean, temporary database for every test."""
    db_file = tmp_path / "test_media.db"
    return DatabaseManager(str(db_file))