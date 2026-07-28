"""Structured logging setup.

Console rendering for humans, JSON for aggregators. Every log line carries the
company slug where one is in scope, which is what makes "why did Caterpillar
stop working" answerable from the log file alone.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, MutableMapping

import structlog

from ..config.schema import LoggingSettings


def _add_severity(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping:
    """Mirror `level` as `severity` for log backends that expect it."""
    if "level" in event_dict:
        event_dict["severity"] = event_dict["level"].upper()
    return event_dict


def configure_logging(settings: LoggingSettings) -> None:
    level = getattr(logging, settings.level)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
        shared_processors.append(_add_severity)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if settings.file:
        path = Path(settings.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=settings.rotate_max_bytes,
            backupCount=settings.rotate_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # These libraries are chatty at INFO and drown out our own lines.
    for noisy in ("httpx", "httpcore", "apscheduler.scheduler", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)


def bind_company(slug: str) -> None:
    structlog.contextvars.bind_contextvars(company=slug)


def clear_company() -> None:
    structlog.contextvars.unbind_contextvars("company")
