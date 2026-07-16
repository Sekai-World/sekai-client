"""
Centralized secret redaction for logs, structured data, and text.

Provides recursive redaction of sensitive keys/values in dict/list/Mapping
structures, regex-based redaction of secrets embedded in free-text
(headers, URL query strings, bearer tokens, dict/list reprs), and a
redacting logging ``Formatter`` so the final rendered line -- including
tracebacks / ``exc_text`` -- is masked before it reaches handlers/files.

Sensitive values are replaced with the literal ``[REDACTED]``.
"""

import logging
import re
from collections.abc import ItemsView, Mapping
from typing import Any

# Literal replacement for any redacted secret.
REDACTED = "[REDACTED]"

# Lower-cased keys whose *values* are always redacted in structured data.
SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-session-token",
    "credential",
    "signature",
    "accesstoken",
    "access_token",
    "token",
    "api_key",
    "x-api-token",
    "x-api-key",
    "device_id",
    "x-install-id",
    "x-internal-rpc-token",
    "internal_rpc_token",
    "x-strapi-token",
    "strapi_token",
}

# URL query / ``key=value`` redaction (token-bearing query params).
_URL_PARAM_RE = re.compile(
    r"([?&](?:" + "|".join(sorted(SENSITIVE_KEYS)) + r"))=[^&\s]+",
    re.IGNORECASE,
)

# Header-like ``Key: value`` / ``Key=value`` redaction.
_HEADER_RE = re.compile(
    r"(?i)\b("
    r"Authorization|Cookie|Set-Cookie|X-Session-Token|"
    r"X-Api-Token|X-Api-Key|X-Install-Id|Device-Id|Access-Token|Api-Key|"
    r"Token|X-Internal-Rpc-Token|Internal-Rpc-Token|X-Strapi-Token|Strapi-Token"
    r")\b[=:]\s*('?)([^'\s]+)('?)",
)

# JSON / Python-repr style sensitive fields, e.g.
# ``{"credential": "SECRET"}`` or ``{'accessToken': 'SECRET'}``.
_QUOTED_FIELD_RE = re.compile(
    r"(?i)(['\"])("
    r"authorization|cookie|set-cookie|x-session-token|credential|signature|"
    r"accessToken|access_token|token|api_key|x-api-token|x-api-key|"
    r"device_id|x-install-id|x-internal-rpc-token|internal_rpc_token|"
    r"x-strapi-token|strapi_token"
    r")\1\s*:\s*(['\"])(.*?)\3"
)

# ``Bearer <token>`` redaction.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")


def _is_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in SENSITIVE_KEYS


def redact_structure(obj: Any) -> Any:
    """
    Recursively redact sensitive values in dict/Mapping/list/tuple structures.

    Dict/Mapping entries whose key is sensitive are replaced with ``[REDACTED]``;
    all other values are recursively walked. ``ItemsView`` (e.g. from
    ``dict.items()``) is treated as a key/value collection and redacted by
    key. Other types are returned as-is.
    """
    if isinstance(obj, (dict, Mapping)):
        return {
            k: (REDACTED if _is_sensitive_key(k) else redact_structure(v))
            for k, v in obj.items()
        }
    if isinstance(obj, ItemsView):
        return {
            k: (REDACTED if _is_sensitive_key(k) else redact_structure(v))
            for k, v in obj
        }
    if isinstance(obj, (list, tuple, set, frozenset)):
        return type(obj)(redact_structure(v) for v in obj)
    return obj


def redact_text(text: str) -> str:
    """
    Redact secrets embedded in free text.

    Handles bearer tokens, header-like ``Key: value`` pairs, ``Key=value``
    pairs (incl. inside dict/list reprs such as ``{'credential': 'SECRET'}``),
    and ``key=value`` query parameters (incl. in URLs).
    """
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    text = _QUOTED_FIELD_RE.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}{m.group(1)}: {m.group(3)}{REDACTED}{m.group(3)}"
        ),
        text,
    )
    text = _HEADER_RE.sub(lambda m: f"{m.group(1)}: {REDACTED}", text)
    text = _URL_PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return text


def _redact_arg(arg: Any) -> Any:
    if isinstance(arg, str):
        return redact_text(arg)
    return redact_structure(arg)


class RedactingFormatter(logging.Formatter):
    """
    Formatter that redacts secrets from the fully rendered record.

    Wraps the base formatting so the final output -- including the message,
    any structured args, ``exc_text`` (tracebacks) and ``stack_info`` --
    is passed through ``redact_text``. Use this on handlers (alongside the
    logger-level ``SecretRedactingFilter``) so secrets never reach files.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact_text(text)


class SecretRedactingFilter(logging.Filter):
    """
    Logging filter that redacts secrets from records.

    Structured arguments (dict/ItemsView/list/tuple/Mapping) are redacted in
    place first, then the fully formatted message is text-redacted so
    header-like / bearer / URL secrets are masked. ``args`` is then cleared
    to avoid re-formatting (which would otherwise raise on redacted
    placeholders). This covers the record message but NOT ``exc_text``; pair
    it with ``RedactingFormatter`` on handlers for traceback coverage.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # A single positional dict arg is stored as ``record.args`` itself
        # (not wrapped in a tuple) by logging.LogRecord. Redact either shape.
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = _redact_arg(record.args)

        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)

        record.msg = redact_text(message)
        record.args = None
        return True


# Module-level singletons so they are only ever attached once.
_REDACTION_FILTER = SecretRedactingFilter()
_REDACTION_FORMATTER = RedactingFormatter()


def attach_redaction(logger: logging.Logger) -> None:
    """
    Attach the secret-redacting filter to a logger without touching handlers.

    Idempotent: does nothing if the filter is already attached. Use this for
    entry points (e.g. gunicorn) that already have handlers configured.
    """
    if _REDACTION_FILTER not in logger.filters:
        logger.addFilter(_REDACTION_FILTER)


def install_redaction_on_handlers(logger: logging.Logger | None = None) -> None:
    """
    Wrap every handler's formatter with a redacting formatter.

    Operates on ``logger`` and, when ``logger`` propagates (the default),
    also on the root logger's handlers, so records from any child logger
    that bubble up to those handlers are redacted -- including tracebacks.
    Idempotent: skips handlers already using a ``RedactingFormatter``.
    """
    if logger is None:
        logger = logging.getLogger()

    targets: list[logging.Logger] = [logger]
    if getattr(logger, "propagate", False) and logger is not logging.getLogger():
        targets.append(logging.getLogger())

    for target in targets:
        for handler in target.handlers:
            if isinstance(handler.formatter, RedactingFormatter):
                continue
            base = handler.formatter or logging.Formatter()
            handler.formatter = RedactingFormatter(
                fmt=base._style._fmt,
                datefmt=base.datefmt,
            )


def enable_log_redaction(logger: logging.Logger | None = None) -> None:
    """
    Enable full log redaction for a logger: filter (args/message) + formatter
    (handlers / tracebacks). Does not clear or add handlers, so it is safe
    for processes (gunicorn) that already have handlers configured.
    """
    if logger is None:
        logger = logging.getLogger()
    attach_redaction(logger)
    install_redaction_on_handlers(logger)
