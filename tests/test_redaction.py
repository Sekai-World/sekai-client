"""Unit tests for centralized secret redaction."""

from utils.redaction import (
    REDACTED,
    SecretRedactingFilter,
    enable_log_redaction,
    redact_structure,
    redact_text,
)


class TestRedactStructure:
    def test_redacts_sensitive_keys_recursively(self):
        data = {
            "authorization": "Bearer secret",
            "x-session-token": "tok",
            "nested": {"credential": "cred", "keep": "value"},
            "list": [{"signature": "sig"}],
        }
        out = redact_structure(data)
        assert out["authorization"] == REDACTED
        assert out["x-session-token"] == REDACTED
        assert out["nested"]["credential"] == REDACTED
        assert out["nested"]["keep"] == "value"
        assert out["list"][0]["signature"] == REDACTED

    def test_non_sensitive_values_preserved(self):
        data = {"userId": "123", "region": "jp", "tokenX": "abc"}
        # 'tokenX' is not an exact sensitive key, so it is preserved.
        out = redact_structure(data)
        assert out == data

    def test_original_structure_not_mutated(self):
        data = {"credential": "cred"}
        redact_structure(data)
        assert data["credential"] == "cred"

    def test_tuple_preserved_as_list(self):
        out = redact_structure(("a", {"token": "x"}))
        assert out == ("a", {"token": REDACTED})

    def test_redacts_dict_items_and_mixed_case_keys(self):
        headers = {"x-internal-rpc-token": "rpc", "accessToken": "access"}
        out = redact_structure(headers.items())
        assert out["x-internal-rpc-token"] == REDACTED
        assert out["accessToken"] == REDACTED

    def test_redacts_camelcase_device_and_install_keys(self):
        data = {
            "deviceId": "dev-123",
            "installId": "inst-456",
            "userId": "u1",
        }
        out = redact_structure(data)
        assert out["deviceId"] == REDACTED
        assert out["installId"] == REDACTED
        assert out["userId"] == "u1"


class TestRedactText:
    def test_redacts_bearer_token(self):
        assert "secret" not in redact_text("Authorization: Bearer secret")
        assert "Bearer [REDACTED]" in redact_text("Bearer secret")

    def test_redacts_header_like(self):
        text = "cookie: abc=def; x-session-token=secret123"
        out = redact_text(text)
        assert "secret123" not in out
        assert "x-session-token: [REDACTED]" in out

    def test_redacts_url_query_token(self):
        url = "https://host/path?token=supersecret&other=1"
        out = redact_text(url)
        assert "supersecret" not in out
        # The token value must be replaced by the redaction marker.
        assert "[REDACTED]" in out

    def test_redacts_url_query_camelcase_device_and_install(self):
        url = "https://host/path?deviceId=dev-123&installId=inst-456&other=1"
        out = redact_text(url)
        assert "dev-123" not in out
        assert "inst-456" not in out
        assert out.count("[REDACTED]") == 2

    def test_redacts_json_and_python_repr_fields(self):
        json_text = '{"credential": "secret", "userId": "1"}'
        repr_text = "{'accessToken': 'secret2', 'region': 'jp'}"

        assert "secret" not in redact_text(json_text)
        assert "secret2" not in redact_text(repr_text)
        assert REDACTED in redact_text(json_text)
        assert REDACTED in redact_text(repr_text)

    def test_plain_text_unchanged(self):
        text = "fetched version info for jp region"
        assert redact_text(text) == text


class TestSecretRedactingFilter:
    def test_filter_redacts_bearer_in_message(self):
        import logging

        record = logging.LogRecord(
            "t",
            logging.INFO,
            "f",
            1,
            "request Authorization: Bearer %s",
            ("supersecret",),
            None,
        )
        SecretRedactingFilter().filter(record)
        assert "supersecret" not in record.msg
        assert "[REDACTED]" in record.msg
        assert record.args is None

    def test_filter_redacts_dict_arg(self):
        import logging

        record = logging.LogRecord(
            "t",
            logging.INFO,
            "f",
            1,
            "account %s",
            ({"credential": "s3cr3t-value", "userId": "1"},),
            None,
        )
        SecretRedactingFilter().filter(record)
        assert "s3cr3t-value" not in record.msg
        assert "[REDACTED]" in record.msg
        assert record.args is None

    def test_filter_redacts_single_dict_arg(self):
        import logging

        # logging stores a single positional dict arg as record.args itself.
        record = logging.LogRecord(
            "t",
            logging.INFO,
            "f",
            1,
            "account %s",
            {"credential": "s3cr3t-value", "userId": "1"},
            None,
        )
        SecretRedactingFilter().filter(record)
        assert "s3cr3t-value" not in record.msg
        assert "[REDACTED]" in record.msg
        assert record.args is None


class TestLoggingIntegration:
    def test_child_logger_handler_output_and_traceback_are_redacted(self):
        import io
        import logging

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger("redaction-integration")
        root.handlers[:] = [handler]
        root.filters[:] = []
        root.propagate = False
        root.setLevel(logging.INFO)
        enable_log_redaction(root)

        child = logging.getLogger("redaction-integration.child")
        child.setLevel(logging.INFO)
        child.propagate = True
        child.info("headers=%s", {"x-session-token": "session-secret", "userId": "1"})
        try:
            raise RuntimeError('{"credential": "trace-secret"}')
        except RuntimeError:
            child.exception("failed with Authorization: Bearer bearer-secret")

        output = stream.getvalue()
        assert "session-secret" not in output
        assert "trace-secret" not in output
        assert "bearer-secret" not in output
        assert REDACTED in output
