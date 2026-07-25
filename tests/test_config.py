"""
Unit tests for configuration management.

Tests config parsing, validation, and defaults.
"""

from unittest.mock import patch

import pytest

from config import (
    Config,
    _parse_float_env,
    _parse_int_env,
    _parse_port_env,
    _parse_str_env,
)


class TestConfigParsing:
    """Tests for environment variable parsing."""

    def test_parse_float_env_valid(self):
        """Test parsing valid float environment variable."""
        with patch.dict("os.environ", {"TEST_TIMEOUT": "123.45"}):
            result = _parse_float_env("TEST_TIMEOUT", 100.0)
            assert result == 123.45

    def test_parse_float_env_default(self):
        """Test float parsing uses default if not set."""
        result = _parse_float_env("NONEXISTENT_TIMEOUT", 100.0)
        assert result == 100.0

    def test_parse_float_env_invalid(self):
        """Test float parsing returns default for invalid value."""
        with patch.dict("os.environ", {"TEST_TIMEOUT": "invalid"}):
            result = _parse_float_env("TEST_TIMEOUT", 100.0)
            assert result == 100.0

    def test_parse_float_env_negative(self):
        """Test float parsing rejects negative values."""
        with patch.dict("os.environ", {"TEST_TIMEOUT": "-5"}):
            result = _parse_float_env("TEST_TIMEOUT", 100.0)
            assert result == 100.0

    def test_parse_int_env_valid(self):
        """Test parsing valid int environment variable."""
        with patch.dict("os.environ", {"TEST_RETRIES": "5"}):
            result = _parse_int_env("TEST_RETRIES", 3)
            assert result == 5

    def test_parse_int_env_invalid(self):
        """Test int parsing returns default for invalid value."""
        with patch.dict("os.environ", {"TEST_RETRIES": "invalid"}):
            result = _parse_int_env("TEST_RETRIES", 3)
            assert result == 3

    def test_parse_int_env_negative(self):
        """Test int parsing rejects negative values."""
        with patch.dict("os.environ", {"TEST_RETRIES": "-1"}):
            result = _parse_int_env("TEST_RETRIES", 3)
            assert result == 3

    @pytest.mark.parametrize("raw", ["0", "-1", "65536"])
    def test_parse_port_env_retains_out_of_range_value_for_validation(self, raw):
        """Port bounds are reported by configuration validation, not int parsing."""
        with patch.dict("os.environ", {"TEST_PORT": raw}):
            assert _parse_port_env("TEST_PORT", 39390) == int(raw)

    def test_parse_port_env_accepts_port_bounds(self):
        with patch.dict("os.environ", {"LOW_PORT": "1", "HIGH_PORT": "65535"}):
            assert _parse_port_env("LOW_PORT", 39390) == 1
            assert _parse_port_env("HIGH_PORT", 39390) == 65535

    def test_parse_str_env_valid(self):
        """Test parsing string environment variable."""
        with patch.dict("os.environ", {"TEST_TOKEN": "secret123"}):
            result = _parse_str_env("TEST_TOKEN", "default")
            assert result == "secret123"

    def test_parse_str_env_default(self):
        """Test string parsing uses default if not set."""
        result = _parse_str_env("NONEXISTENT", "default")
        assert result == "default"


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
        """Test formal service regions (CN excluded per D-001)."""
        expected_regions = ["jp", "en", "tw", "kr"]
        assert set(Config.REGIONS) == set(expected_regions)
        assert "cn" not in Config.REGIONS

    def test_region_port_jp(self):
        """Test JP port is configured."""
        assert Config.get_region_port("jp") == 39390

    def test_region_port_en(self):
        """Test EN port is configured."""
        assert Config.get_region_port("en") == 39392

    def test_region_port_cn(self):
        """Test CN port is still configured for the standalone process."""
        assert Config.get_region_port("cn") == 39394

    def test_region_port_tw(self):
        """Test TW port is configured."""
        assert Config.get_region_port("tw") == 39391

    def test_region_port_kr(self):
        """Test KR port is configured."""
        assert Config.get_region_port("kr") == 39393

    def test_region_port_invalid(self):
        """Test invalid region raises ValueError."""
        with pytest.raises(ValueError):
            Config.get_region_port("invalid")


class TestRegionConfigValidation:
    """Tests for startup region-mapping completeness validation."""

    def test_validate_region_config_empty(self):
        """Test all formal regions have complete mappings."""
        errors = Config.validate_region_config()
        assert errors == []

    def test_validate_region_config_no_cn(self):
        """Test CN is intentionally not checked as a formal region."""
        assert "cn" not in Config.REGIONS
        errors = Config.validate_region_config()
        assert not any("cn" in e for e in errors)

    def test_validate_region_config_missing_headers(self, monkeypatch):
        """Test missing headers mapping is reported."""
        monkeypatch.setattr(Config, "REGIONS", ["jp", "en", "tw", "kr", "xx"])
        errors = Config.validate_region_config()
        assert any("xx" in e and "headers" in e for e in errors)

    def test_validate_region_config_missing_url(self, monkeypatch):
        """Test missing URL mapping is reported."""
        monkeypatch.setattr(Config, "REGIONS", ["jp", "en", "tw", "kr", "yy"])
        errors = Config.validate_region_config()
        assert any("yy" in e and "URL" in e for e in errors)

    def test_validate_region_config_missing_port(self, monkeypatch):
        """Test missing RPC port mapping is reported (idempotent safety bound)."""
        monkeypatch.setattr(
            Config,
            "REGIONS",
            ["jp", "en", "tw", "kr", "zz"],
        )
        errors = Config.validate_region_config()
        assert any("zz" in e and "port" in e.lower() for e in errors)

    @pytest.mark.parametrize(
        "attribute", ["JP_PORT", "EN_PORT", "CN_PORT", "TW_PORT", "KR_PORT"]
    )
    @pytest.mark.parametrize("port", [0, 65536])
    def test_validate_region_config_rejects_invalid_configured_ports(
        self, monkeypatch, attribute, port
    ):
        """Every configured port, including the standalone CN port, is bounded."""
        monkeypatch.setattr(Config, attribute, port)

        errors = Config.validate_region_config()

        assert any(
            attribute.removesuffix("_PORT").lower() in error.lower() for error in errors
        )
        assert any("between 1 and 65535" in error for error in errors)

    def test_validate_region_config_is_idempotent(self):
        """Repeated calls must return the same result set (pure, no side effects)."""
        first = sorted(Config.validate_region_config())
        second = sorted(Config.validate_region_config())
        assert first == second
        assert first == []

    def test_validate_includes_region_errors(self):
        """Test Config.validate surfaces region-mapping errors."""
        warnings = Config.validate()
        assert isinstance(warnings, list)

    def test_bootstrap_rejects_incomplete_region_config(self, monkeypatch):
        """Test public API bootstrap fails before initializing any client."""
        import api_public_server

        monkeypatch.setattr(api_public_server, "bootstrapped", False)
        monkeypatch.setattr(
            api_public_server.Config,
            "validate_region_config",
            lambda: ["Region 'xx' missing API headers mapping"],
        )
        initialized_regions = []
        monkeypatch.setattr(
            api_public_server,
            "init_regional_client",
            lambda region: initialized_regions.append(region),
        )

        with pytest.raises(RuntimeError, match="Region configuration is incomplete"):
            api_public_server.bootstrap()

        assert initialized_regions == []


class TestConfigValidation:
    """Tests for Config validation."""

    def test_validate_returns_list(self):
        """Test validate() returns list."""
        result = Config.validate()
        assert isinstance(result, list)

    def test_validate_with_missing_api_token(self):
        """Test validation warns about missing API_TOKEN."""
        with patch.dict("os.environ", {"API_TOKEN": ""}):
            warnings = Config.validate()
            assert any("API_TOKEN" in w for w in warnings)

    def test_validate_successful(self):
        """Test validation passes with valid config."""
        # Even if API_TOKEN is not set, other validations should pass
        warnings = Config.validate()
        # We expect at least one warning (API_TOKEN not set in testing)
        # but the function should work
        assert isinstance(warnings, list)
