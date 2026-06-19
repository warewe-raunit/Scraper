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
    # Compact label for the console: just the last dotted component
    # (api.services.linkedin -> linkedin), so lines stay short and readable —
    # especially on the server where journald prepends its own timestamp+host.
    full = extra.get("logger_name") or record["name"] or ""
    extra["short"] = full.rsplit(".", 1)[-1]
    ctx = {
        k: v
        for k, v in extra.items()
        if k not in ("logger_name", "ctx", "short", "ctx_sep") and v is not None
    }
    extra["ctx"] = " ".join(f"{k}={v}" for k, v in ctx.items())


# Time-only (journald already stamps the full date on the server) + short logger
# name keeps each line compact and scannable.
_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{extra[short]}</cyan> | "
    "<level>{message}</level>"
    "<dim>{extra[ctx_sep]}{extra[ctx]}</dim>"
)


# High-frequency, low-signal events suppressed by default to cut log volume (and
# the latency of emitting them). WARNING/ERROR are NEVER suppressed — only these
# chatty INFO/DEBUG events are. Override the set with LOG_SUPPRESS_EVENTS (comma
# separated) or disable suppression entirely with LOG_VERBOSE=1.
_DEFAULT_SUPPRESS_EVENTS = ",".join([
    "request_started",
    "voyager_get.account_acquired",
    "voyager_get.cleared_login_proxy_cooldown",
    "voyager_fetch.attempt",
    "voyager_fetch.vet_batch",
    "voyager_fetch.success",
    "using_proxy_for_x_profile",
    "using_proxy_for_x_search",
    "using_proxy_for_x_thread",
    "reddit_api_response_received",
])

_WARNING_NO = 30  # loguru: WARNING=30; anything >= is always kept


def _make_noise_filter():
    """Build a Loguru sink filter that drops the chatty events above unless
    LOG_VERBOSE is set. Records at WARNING+ always pass, so problems are never
    hidden — this only trims repetitive INFO/DEBUG progress lines."""
    if os.getenv("LOG_VERBOSE", "0").lower() in ("1", "true", "yes", "on"):
        return None  # keep everything
    raw = os.getenv("LOG_SUPPRESS_EVENTS", _DEFAULT_SUPPRESS_EVENTS)
    suppressed = {e.strip() for e in raw.split(",") if e.strip()}
    if not suppressed:
        return None

    def _filter(record) -> bool:
        if record["level"].no >= _WARNING_NO:
            return True
        return record["message"] not in suppressed

    return _filter


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

    # colorize: unset -> None (auto-detect: TTY yes, pipe no). LOG_COLOR=1 forces
    # ANSI on even through a pipe (journald/systemd shows color); LOG_COLOR=0 forces off.
    _color_env = os.getenv("LOG_COLOR")
    colorize = None if _color_env is None else _color_env.lower() in ("1", "true", "yes", "on")

    root = Path(__file__).resolve().parents[1]
    log_dir = Path(os.getenv("LOG_DIR", str(root / "logs")))
    log_file = Path(os.getenv("LOG_FILE", str(log_dir / "app.log")))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Stamp the PID into the filename so each process rotates its OWN file.
    # uvicorn spawns a worker/reloader subprocess; if every process adds a sink to
    # the SAME path, rotation does os.rename() on a file another process holds
    # open, which raises PermissionError (WinError 32) on Windows and spams the
    # console on every log line. Per-PID files keep rotation collision-free across
    # platforms. Opt out with LOG_FILE_PER_PROCESS=0 (e.g. single-process deploys).
    if os.getenv("LOG_FILE_PER_PROCESS", "1").lower() not in ("0", "false", "no", "off"):
        log_file = log_file.with_name(f"{log_file.stem}.{os.getpid()}{log_file.suffix}")

    loguru_logger.remove()
    loguru_logger.configure(patcher=_console_patcher)

    # Drop the chatty high-frequency events from BOTH sinks (less noise, less
    # I/O latency). WARNING+ always survives. LOG_VERBOSE=1 restores everything.
    noise_filter = _make_noise_filter()

    # stdout sink: pretty colored console, or serialized JSON when LOG_FORMAT=json.
    # colorize is left to auto-detect (None): a TTY (local dev) gets color; a pipe
    # (systemd/journald on the server) gets clean plain text instead of raw ANSI
    # escape codes cluttering the journal.
    if log_format == "json":
        loguru_logger.add(sys.stdout, level=level, serialize=True, enqueue=True,
                          filter=noise_filter)
    else:
        loguru_logger.add(
            sys.stdout,
            level=level,
            format=_CONSOLE_FORMAT,
            colorize=colorize,
            enqueue=True,
            backtrace=False,
            diagnose=False,
            filter=noise_filter,
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
        filter=noise_filter,
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
