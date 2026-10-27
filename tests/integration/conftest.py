"""
Integration test fixtures for Playwright browser tests.
Tests run against a live Flask server (either docker-compose.test.yml or local).
"""
import os
import time

import pytest
import requests


DEFAULT_BASE_URL = 'http://localhost:5099'


@pytest.fixture(scope="session")
def base_url(pytestconfig):
    """Honor TEST_BASE_URL first, then pytest-playwright's --base-url."""
    return (os.getenv('TEST_BASE_URL')
            or pytestconfig.getoption('base_url')
            or DEFAULT_BASE_URL)

@pytest.fixture(scope="session", autouse=True)
def ensure_server(base_url):
    """Fail clearly when the live server never becomes healthy."""
    max_retries = 30
    for _ in range(max_retries):
        try:
            resp = requests.get(f"{base_url}/auth/health", timeout=2)
            if resp.status_code == 200:
                return base_url
        except requests.RequestException:
            pass
        time.sleep(1)
    pytest.fail(f"Test server not available at {base_url}")
