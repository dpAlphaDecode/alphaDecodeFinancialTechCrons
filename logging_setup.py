"""
Shared logging configuration for every script/module in this repo.

Call `configure_logging("<name>")` once, near the top of a script's
`main()`/`if __name__ == "__main__":` block (after `load_dotenv()`), before
any logging happens. It configures the *root* logger, so every module's
`logging.getLogger(__name__)` calls -- no matter how deep the call stack --
flow through the same handlers without each file configuring anything
itself.

Writes to both stdout (visible when run interactively, or captured by
cron/systemd output redirection) and a daily-rotating file under LOG_DIR
(default "logs"), named "<name>.log" so each entry point's log lands in its
own obviously-named file instead of one firehose shared by everything.

LOG_LEVEL / LOG_DIR are read from the environment (.env) each call, same as
DATABASE_URL in db/session.py.

Idempotent: safe to call more than once per process (e.g. once from
scheduler/runner.py and again from a job module it imports) -- a second call
with the same name is a no-op; a second call with a *different* name adds an
extra file handler for that name without duplicating the stream handler.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_configured_file_handlers: set[str] = set()
_stream_configured = False


def configure_logging(log_name: str | None = None) -> logging.Logger:
    global _stream_configured

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_dir = os.environ.get("LOG_DIR", "logs")
    name = log_name or "app"

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    if not _stream_configured:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)
        _stream_configured = True

    if name not in _configured_file_handlers:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            os.path.join(log_dir, f"{name}.log"),
            when="midnight",
            backupCount=14,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        _configured_file_handlers.add(name)

    return root
