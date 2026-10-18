# logging_config.py / logging_config,ini
import logging
import logging.config
import sys
from typing import Any
import json
from utilities.security import get_current_correlation_id


class JSONFormatter(logging.Formatter):
    """Formats log records as structured JSON for log aggregators (ELK, CloudWatch, Datadog)."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func_name": record.funcName,
            "line_no": record.lineno,
            "correlation_id": get_current_correlation_id(),
        }

        # Include exception traces if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Capture extra fields passed via logger.info("...", extra={"user_id": 123})
        if hasattr(record, "extra_data"):
            log_data["extra"] = record.extra_data

        return json.dumps(log_data)


def get_logging_config(
    log_level: str = "INFO", environment: str = "production"
) -> dict[str, Any]:
    """Generates a dictConfig dictionary based on the runtime environment."""

    # Use JSON formatting in staging/production; human-readable text in dev
    formatter_type = (
        "json" if environment in ("production", "staging") else "console_readable"
    )

    return {
        "version": 1,
        "disable_existing_loggers": False,  # Keeps default framework loggers active
        "formatters": {
            "json": {
                "()": JSONFormatter,
            },
            "console_readable": {
                "format": "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": formatter_type,
                "stream": sys.stdout,  # Logs strictly to stdout for containerized environments (Docker/K8s)
            },
        },
        "loggers": {
            # Root Logger
            "": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": True,
            },
            # Silence or adjust noisy third-party libraries
            "uvicorn.access": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "sqlalchemy.engine": {
                "handlers": ["default"],
                "level": "WARNING",  # Avoid spamming SQL queries in high log levels unless debugging
                "propagate": False,
            },
        },
    }


def setup_logging(log_level: str = "INFO", environment: str = "production") -> None:
    """Initializes logging configuration for the application startup."""
    config = get_logging_config(log_level=log_level, environment=environment)
    logging.config.dictConfig(config)
