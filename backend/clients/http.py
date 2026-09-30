"""Shared HTTP helper: one retry with jitter, honouring 429 Retry-After."""

import logging
import random
import time
from typing import Optional, Sequence

import httpx

from backend.errors import AppError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_RETRY_WAIT_S = 5.0


def _retry_wait(resp: Optional[httpx.Response]) -> float:
    wait = 0.5 + random.random()  # jitter
    if resp is not None and resp.status_code == 429:
        try:
            wait = max(wait, float(resp.headers.get("Retry-After", "1")))
        except ValueError:
            pass
    return min(wait, MAX_RETRY_WAIT_S)


def request_json(
    client: httpx.Client,
    method: str,
    urls: Sequence[str],
    *,
    provider: str,
    **kwargs,
):
    """Try urls[0]; on a transient failure retry once (on urls[1] if given).

    Returns parsed JSON or raises AppError with a safe message.
    """
    attempts = [urls[0], urls[1] if len(urls) > 1 and urls[1] else urls[0]]
    last_error: Optional[AppError] = None
    for i, url in enumerate(attempts):
        resp: Optional[httpx.Response] = None
        try:
            resp = client.request(method, url, **kwargs)
            if resp.status_code in RETRYABLE_STATUS:
                busy = resp.status_code == 429
                last_error = AppError(
                    "provider_busy" if busy else "provider_unavailable",
                    f"The public {provider} service is busy right now. Please retry in a minute."
                    if busy else f"The {provider} service is temporarily unavailable. Please retry.",
                    retryable=True, provider=provider, http_status=503,
                )
            elif resp.status_code == 403:
                raise AppError(
                    "provider_forbidden",
                    f"{provider} refused the request (check HTTP_USER_AGENT in .env).",
                    provider=provider, http_status=502,
                )
            elif resp.status_code >= 400:
                raise AppError(
                    "provider_error", f"{provider} returned an error ({resp.status_code}).",
                    retryable=resp.status_code >= 500, provider=provider, http_status=502,
                )
            else:
                try:
                    return resp.json()
                except ValueError:
                    raise AppError(
                        "provider_bad_response", f"{provider} returned an unreadable response.",
                        retryable=True, provider=provider, http_status=502,
                    )
        except httpx.TimeoutException:
            last_error = AppError(
                "provider_timeout", f"{provider} took too long to respond. Please retry.",
                retryable=True, provider=provider, http_status=504,
            )
        except httpx.TransportError:
            last_error = AppError(
                "provider_unreachable", f"Could not reach {provider}. Check your internet connection.",
                retryable=True, provider=provider, http_status=503,
            )
        log.warning("provider=%s attempt=%d code=%s", provider, i + 1, last_error.code)
        if i == 0:
            time.sleep(_retry_wait(resp))
    raise last_error
