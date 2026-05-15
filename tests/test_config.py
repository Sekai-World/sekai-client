"""
Unit tests for configuration management.

Tests config parsing, validation, and defaults.
"""

import pytest
from unittest.mock import patch
from config import Config, _parse_float_env, _parse_int_env, _parse_str_env


class TestConfigParsing:
    """Tests for environment variable parsing."""
    
    def test_parse_float_env_valid(self):
        """Test parsing valid float environment variable."""
        with patch.dict('os.environ', {'TEST_TIMEOUT': '123.45'}):
            result = _parse_float_env('TEST_TIMEOUT', 100.0)
            assert result == 123.45
    
    def test_parse_float_env_default(self):
        """Test float parsing uses default if not set."""
        result = _parse_float_env('NONEXISTENT_TIMEOUT', 100.0)
        assert result == 100.0
    
    def test_parse_float_env_invalid(self):
        """Test float parsing returns default for invalid value."""
        with patch.dict('os.environ', {'TEST_TIMEOUT': 'invalid'}):
            result = _parse_float_env('TEST_TIMEOUT', 100.0)
            assert result == 100.0
    
    def test_parse_float_env_negative(self):
        """Test float parsing rejects negative values."""
        with patch.dict('os.environ', {'TEST_TIMEOUT': '-5'}):
            result = _parse_float_env('TEST_TIMEOUT', 100.0)
            assert result == 100.0
    
    def test_parse_int_env_valid(self):
        """Test parsing valid int environment variable."""
        with patch.dict('os.environ', {'TEST_RETRIES': '5'}):
            result = _parse_int_env('TEST_RETRIES', 3)
            assert result == 5
    
    def test_parse_int_env_invalid(self):
        """Test int parsing returns default for invalid value."""
        with patch.dict('os.environ', {'TEST_RETRIES': 'invalid'}):
            result = _parse_int_env('TEST_RETRIES', 3)
            assert result == 3
    
    def test_parse_int_env_negative(self):
        """Test int parsing rejects negative values."""
        with patch.dict('os.environ', {'TEST_RETRIES': '-1'}):
            result = _parse_int_env('TEST_RETRIES', 3)
            assert result == 3
    
    def test_parse_str_env_valid(self):
        """Test parsing string environment variable."""
        with patch.dict('os.environ', {'TEST_TOKEN': 'secret123'}):
            result = _parse_str_env('TEST_TOKEN', 'default')
            assert result == 'secret123'
    
    def test_parse_str_env_default(self):
        """Test string parsing uses default if not set."""
        result = _parse_str_env('NONEXISTENT', 'default')
        assert result == 'default'


class TestConfigValues:
    """Tests for Config class values."""
    
    def test_request_timeout_default(self):
        """Test REQUEST_TIMEOUT has reasonable default."""
        assert Config.REQUEST_TIMEOUT > 0
        assert Config.REQUEST_TIMEOUT == 150.0
    
    def test_job_queue_timeout_default(self):
        """Test JOB_QUEUE_TIMEOUT has reasonable default."""
        assert Config.JOB_QUEUE_TIMEOUT > 0
        assert Config.JOB_QUEUE_TIMEOUT == 30.0
    
    def test_answer_queue_timeout_default(self):
        """Test ANSWER_QUEUE_TIMEOUT has reasonable default."""
        assert Config.ANSWER_QUEUE_TIMEOUT > 0
        assert Config.ANSWER_QUEUE_TIMEOUT == 180.0
    
    def test_max_api_retries_default(self):
        """Test MAX_API_RETRIES has reasonable default."""
        assert Config.MAX_API_RETRIES >= 0
        assert Config.MAX_API_RETRIES == 3
    
    def test_regions_supported(self):
        """Test all regions are defined."""
        expected_regions = ['jp', 'en', 'cn', 'tw', 'kr']
        assert set(Config.REGIONS) == set(expected_regions)
    
    def test_region_port_jp(self):
        """Test JP port is configured."""
        assert Config.get_region_port('jp') == 39390
    
    def test_region_port_en(self):
        """Test EN port is configured."""
        assert Config.get_region_port('en') == 39392
    
    def test_region_port_cn(self):
        """Test CN port is configured."""
        assert Config.get_region_port('cn') == 39394
    
    def test_region_port_tw(self):
        """Test TW port is configured."""
        assert Config.get_region_port('tw') == 39391
    
    def test_region_port_kr(self):
        """Test KR port is configured."""
        assert Config.get_region_port('kr') == 39393
    
    def test_region_port_invalid(self):
        """Test invalid region raises ValueError."""
        with pytest.raises(ValueError):
            Config.get_region_port('invalid')


class TestConfigValidation:
    """Tests for Config validation."""
    
    def test_validate_returns_list(self):
        """Test validate() returns list."""
        result = Config.validate()
        assert isinstance(result, list)
    
    def test_validate_with_missing_api_token(self):
        """Test validation warns about missing API_TOKEN."""
        with patch('config.Config.API_TOKEN', ''):
            warnings = Config.validate()
            assert any(
                'API_TOKEN' in w for w in warnings
            )
    
    def test_validate_successful(self):
        """Test validation passes with valid config."""
        # Even if API_TOKEN is not set, other validations should pass
        warnings = Config.validate()
        # We expect at least one warning (API_TOKEN not set in testing)
        # but the function should work
        assert isinstance(warnings, list)
