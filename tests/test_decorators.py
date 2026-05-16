"""
Unit tests for Flask decorators.

Tests API key authentication and authorization.
"""

import pytest
from unittest.mock import patch, Mock
from flask import Flask, request

from utils.decorators import require_apikey


@pytest.fixture
def app():
    """Provide a Flask app for testing."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    
    @app.route('/protected', methods=['GET', 'POST'])
    @require_apikey
    def protected():
        return {'status': 'success'}
    
    return app


class TestRequireApiKey:
    """Tests for the require_apikey decorator."""
    
    def test_decorated_function_called(self, app):
        """Test decorated function is called when auth passes."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': 'test_token_123'}):
                response = client.get(
                    '/protected',
                    headers={'x-api-token': 'test_token_123'}
                )
                assert response.status_code == 200
                assert response.json == {'status': 'success'}
    
    def test_missing_api_token_env(self, app):
        """Test returns 500 when API_TOKEN not configured."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': ''}):
                response = client.get(
                    '/protected',
                    headers={'x-api-token': 'any_token'}
                )
                assert response.status_code == 500
    
    def test_missing_request_token_header(self, app):
        """Test returns 401 when request token header missing."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': 'test_token_123'}):
                response = client.get('/protected')
                assert response.status_code == 401
    
    def test_wrong_token(self, app):
        """Test returns 401 when token doesn't match."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': 'correct_token'}):
                response = client.get(
                    '/protected',
                    headers={'x-api-token': 'wrong_token'}
                )
                assert response.status_code == 401
    
    def test_wrong_tokens_consistently_rejected(self, app):
        """Test wrong tokens are consistently rejected."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': 'test_token_123'}):
                # Both should return 401 with same timing behavior
                response1 = client.get(
                    '/protected',
                    headers={'x-api-token': 'a' * 13}
                )
                response2 = client.get(
                    '/protected',
                    headers={'x-api-token': 'z' * 13}
                )
                assert response1.status_code == 401
                assert response2.status_code == 401
    
    def test_token_case_sensitive(self, app):
        """Test token comparison is case-sensitive."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': 'TEST_TOKEN'}):
                response = client.get(
                    '/protected',
                    headers={'x-api-token': 'test_token'}
                )
                assert response.status_code == 401
    
    def test_post_request_protected(self, app):
        """Test decorator works on POST requests."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': 'test_token_123'}):
                response = client.post(
                    '/protected',
                    headers={'x-api-token': 'test_token_123'}
                )
                assert response.status_code == 200
    
    def test_empty_token_header_fails(self, app):
        """Test empty token header is treated as invalid."""
        with app.test_client() as client:
            with patch.dict('os.environ', {'API_TOKEN': 'test_token_123'}):
                response = client.get(
                    '/protected',
                    headers={'x-api-token': ''}
                )
                assert response.status_code == 401
    
    def test_api_token_read_at_request_time(self, app):
        """Test API_TOKEN is read at request time (not import time)."""
        with app.test_client() as client:
            # Start without token
            with patch.dict('os.environ', {'API_TOKEN': ''}):
                response = client.get(
                    '/protected',
                    headers={'x-api-token': 'token'}
                )
                assert response.status_code == 500
            
            # Change token and retry - should work if read at request time
            with patch.dict('os.environ', {'API_TOKEN': 'token'}):
                response = client.get(
                    '/protected',
                    headers={'x-api-token': 'token'}
                )
                assert response.status_code == 200
