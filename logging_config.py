"""
Centralized logging configuration for sekai-client.

Provides standardized logging setup with consistent formatting across
all modules. Use configure_logging() to initialize at application startup.
"""

import logging
import sys

# Standard log format with structured information
LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s"
)

# Simplified format for console output
CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"

# Date format for logs
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    level: int | str = logging.INFO, log_file: str | None = None, console: bool = True
) -> None:
    """
    Configure logging for the entire application.

    Sets up root logger with both console and optional file handlers.
    Use this function at application startup.

    Args:
        level: Logging level (int or string like 'INFO', 'DEBUG')
        log_file: Optional path to log file. If provided, logs to file.
        console: Whether to log to console (default: True)

    Example:
        >>> configure_logging(level=logging.DEBUG, log_file='app.log')
    """
    # Normalize level to int
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_formatter = logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT)
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(level)
            file_formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
        except OSError as e:
            root_logger.error("Failed to set up file logging to %s: %s", log_file, e)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module.

    Convenience function that wraps logging.getLogger().

    Args:
        name: Logger name, typically __name__

    Returns:
        Logger instance

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info('Application started')
    """
    return logging.getLogger(name)
