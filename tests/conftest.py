import os
import tempfile

# Must be set before the app module is imported so tests never touch the real data/ directory.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="geoapi_tests_")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
