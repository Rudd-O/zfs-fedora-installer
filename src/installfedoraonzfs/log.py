"""Logging functionality."""

import logging
from pathlib import Path
import time
from typing import Any

BASIC_FORMAT = "%(asctime)8s  %(levelname)2s  %(message)s"
TRACE_FORMAT = (
    "%(asctime)8s  %(levelname)2s:%(name)16s:%(funcName)32s@%(lineno)4d\t%(message)s"
)


def log_config(trace_file: Path | None = None) -> None:
    """Set up logging formats."""
    logging.addLevelName(logging.DEBUG, "TT")
    logging.addLevelName(logging.INFO, "II")
    logging.addLevelName(logging.WARNING, "WW")
    logging.addLevelName(logging.ERROR, "EE")
    logging.addLevelName(logging.CRITICAL, "XX")

    class TimeFormatter(logging.Formatter):
        def __init__(self, *a: Any, **kw: Any) -> None:
            logging.Formatter.__init__(self, *a, **kw)
            self.start = time.time()

        def formatTime(
            self, record: logging.LogRecord, datefmt: str | None = None
        ) -> str:
            t = time.time() - self.start
            m = int(t / 60)
            s = t % 60
            return "%dm%.2f" % (m, s)

    rl = logging.getLogger()
    rl.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    cfm = TimeFormatter(BASIC_FORMAT)
    ch.setLevel(logging.INFO)
    ch.setFormatter(cfm)
    rl.addHandler(ch)
    if trace_file:
        th = logging.FileHandler(trace_file, mode="w")
        tfm = TimeFormatter(TRACE_FORMAT)
        th.setLevel(logging.DEBUG)
        th.setFormatter(tfm)
        rl.addHandler(th)
