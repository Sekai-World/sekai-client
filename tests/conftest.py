"""
Pytest configuration and fixtures.

Provides common fixtures for testing sekai-client components.
"""

import logging
import queue as queue_module
from unittest.mock import Mock

import pytest


@pytest.fixture
def mock_logger():
    """Provide a mock logger for testing."""
    return Mock(spec=logging.Logger)


@pytest.fixture
def queue():
    """Provide a fresh queue instance."""
    return queue_module.Queue(maxsize=1)


@pytest.fixture
def response_queue():
    """Provide a response queue for testing job queue."""
    return queue_module.Queue(maxsize=1)


@pytest.fixture
def config_env():
    """
    Fixture to test configuration parsing.

    Provides a clean environment for config tests.
    """
    return {
        "REQUEST_TIMEOUT": "150",
        "JOB_QUEUE_TIMEOUT": "30",
        "ANSWER_QUEUE_TIMEOUT": "180",
        "MAX_API_RETRIES": "3",
        "API_TOKEN": "test_token_123",
        "LOGLEVEL": "INFO",
    }


@pytest.fixture
def mock_api_client(mock_logger):
    """Provide a mock API client."""
    from api_client import APIClient

    client = Mock(spec=APIClient)
    client.logger = mock_logger
    client.region = "jp"
    client.account_info = {}
    client.version_info = {}
    client.user_info = {}
    client.rate_limited = False

    return client


@pytest.fixture
def mock_flask_app():
    """Provide a mock Flask app."""
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True
    return app
