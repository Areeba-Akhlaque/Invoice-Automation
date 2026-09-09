"""EXECUTION — shared retry for Google API calls.

The scheduled run used to die on transient DNS blips and connection resets (see
scheduled.log), leaving no invoice and no alert. Every Sheets and Calendar call
goes through _retry.
"""
from __future__ import annotations

import random
import socket
import ssl
import time

import requests
from googleapiclient.errors import HttpError

RETRYABLE_EXC = (
    socket.gaierror,
    ssl.SSLError,
    TimeoutError,
    ConnectionError,
    requests.exceptions.RequestException,
    OSError,
)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def is_retryable_http(e: HttpError) -> bool:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", None)
    return status in RETRYABLE_STATUS


def retry(fn, *, attempts: int = 4, what: str = "API call"):
    """Run fn() with exponential backoff on transient network/API errors."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except HttpError as e:
            if not is_retryable_http(e):
                raise  # 400/403/404 are real bugs — fail fast, don't mask them
            last = e
        except RETRYABLE_EXC as e:
            last = e
        if i < attempts - 1:
            delay = 2**i + random.uniform(0, 0.5)
            print(f"  ! {what} failed ({str(last)[:90]}) — retry {i + 1}/{attempts - 1} in {delay:.1f}s")
            time.sleep(delay)
    raise last  # type: ignore[misc]
