"""Diagnostic log of connection events (dropouts, rejoins, reconnect requests),
kept in %APPDATA%\\RoomCast\\roomcast.log so a dropout can be looked at after
the fact. Small and rotated, so it never grows without bound."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "RoomCast" / "roomcast.log"

log = logging.getLogger("roomcast")


def setup():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")
    except OSError:
        log.addHandler(logging.NullHandler())
        return
    h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d  %(threadName)-14s %(message)s",
                                     "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
