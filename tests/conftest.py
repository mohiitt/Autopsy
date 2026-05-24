"""Pytest config: each test uses a fresh temp session dir."""
import tempfile

import pytest


@pytest.fixture(autouse=True)
def autopsy_temp_session_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="autopsy_pytest_")
    monkeypatch.setenv("AUTOPSY_SESSION_DIR", tmp)
    yield tmp
