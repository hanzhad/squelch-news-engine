"""Logging that reads well in the Actions console."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    level = os.environ.get("AETHERFEED_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # These are chatty at DEBUG and never say anything we need.
    for noisy in ("httpx", "httpcore", "trafilatura", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
