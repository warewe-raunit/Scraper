"""
tools/logging_config.py — Central logging setup: Loguru sink, structlog API.

Design (chosen deliberately):
  * structlog stays the API. Every module keeps calling
    ``structlog.get_logger(__name__).info("event_name", key=value)``. None of the
    existing call sites change, so all structured context is preserved.
  * Loguru is the sink + renderer. It owns *where* logs go (colored stdout and a
    rotating, compressed JSON file) and the final formatting.

How they connect: structlog runs its processor chain to build the event dict,
then the final processor (`_LoguruEmitter`) forwards the event message plus its
structured fields to Loguru and raises ``structlog.DropEvent`` so the stdlib
logger chain is bypassed (no double logging).

Environment variables:
  LOG_LEVEL   minimum level (default "INFO")
  LOG_FORMAT  "console" (default, pretty colored) or "json" (stdout serialized)
  LOG_FILE    path to the rotating log file (default "<repo>/logs/app.log")
  LOG_DIR     directory for the default log file (default "<repo>/logs")
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import structlog
from loguru import logger as loguru_logger

_configured = False


def _is_known_level(name: str) -> bool:
    try:
        loguru_logger.level(name)
        return True
    except ValueError:
        return False


class _LoguruEmitter:
    """Final structlog processor: emit through Loguru, then drop the event."""

    def __call__(self, _logger: Any, method_name: str, event_dict: Dict[str, Any]):
        event = event_dict.pop("event", "")
        level = str(event_dict.pop("level", method_name)).upper()
        if not _is_known_level(level):
            level = "INFO"

        # Loguru stamps its own timestamp; drop structlog's to avoid duplicates.
        event_dict.pop("timestamp", None)
        logger_name = event_dict.pop("logger", None) or event_dict.pop("logger_name", None)

        # structlog's format_exc_info renders tracebacks into "exception"; append
        # it to the message so Loguru prints the full stack.
        exception = event_dict.pop("exception", None)
        message = event if not exception else f"{event}\n{exception}"

        # Anything still in the dict is structured context -> bind it so both the
        # console renderer and the serialized JSON sink carry the fields. The
        # accurate module name comes from structlog (logger_name), so we don't
        # rely on Loguru's own caller introspection here.
        loguru_logger.bind(logger_name=logger_name, **event_dict).log(level, message)

        raise structlog.DropEvent


def _patcher(record) -> None:
    """Compute display fields on each record for the console format string."""
    extra = record["extra"]
    extra.setdefault("logger_name", record["name"])
    ctx = {
        k: v
        for k, v in extra.items()
        if k not in ("logger_name", "ctx") and v is not None
    }
    extra["ctx"] = " ".join(f"{k}={v}" for k, v in ctx.items())


_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[logger_name]}</cyan> - "
    "<level>{message}</level>"
    "<dim>{extra[ctx_sep]}{extra[ctx]}</dim>"
)


def _console_patcher(record) -> None:
    _patcher(record)
    # Only prefix a separator when there is context to show.
    record["extra"]["ctx_sep"] = "  " if record["extra"].get("ctx") else ""


def configure_logging() -> None:
    """Configure Loguru sinks and point structlog at the Loguru emitter.

    Idempotent: safe to call more than once (e.g. app import + worker reload).
    """
    global _configured
    if _configured:
        return

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "console").lower()

    root = Path(__file__).resolve().parents[1]
    log_dir = Path(os.getenv("LOG_DIR", str(root / "logs")))
    log_file = Path(os.getenv("LOG_FILE", str(log_dir / "app.log")))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    loguru_logger.remove()
    loguru_logger.configure(patcher=_console_patcher)

    # stdout sink: pretty colored console, or serialized JSON when LOG_FORMAT=json.
    if log_format == "json":
        loguru_logger.add(sys.stdout, level=level, serialize=True, enqueue=True)
    else:
        loguru_logger.add(
            sys.stdout,
            level=level,
            format=_CONSOLE_FORMAT,
            colorize=True,
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )

    # Rotating, compressed JSON file sink — durable, machine-readable history.
    loguru_logger.add(
        str(log_file),
        level=level,
        serialize=True,
        rotation=os.getenv("LOG_ROTATION", "10 MB"),
        retention=os.getenv("LOG_RETENTION", "10 days"),
        compression="zip",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _LoguruEmitter(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    _configured = True
