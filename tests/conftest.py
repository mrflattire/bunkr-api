import asyncio
import sys

import pytest
import sqlite3
from bunkr_api.core.db import DatabaseManager


def pytest_asyncio_loop_factories(config, item):
    """Use the Selector event loop on Windows to avoid CurlCffi Proactor warnings.

    pytest-asyncio's own docs now recommend this hook over overriding the
    event_loop_policy fixture (deprecated). Passing the loop class directly
    also avoids asyncio's policy system, which is deprecated in Python 3.14
    and slated for removal in 3.16.
    """
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest.fixture
def temp_db(tmp_path):
    """Provides a clean, temporary database for every test."""
    db_file = tmp_path / "test_media.db"
    return DatabaseManager(str(db_file))