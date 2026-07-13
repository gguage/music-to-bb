from __future__ import annotations

import random
import time
import threading
from dataclasses import dataclass
from typing import Optional

import httpx


class TokenBucketLimiter:
    def __init__(self, rate: float, burst: int | None = None):
        self._rate = rate
        self._burst = burst if burst is not None else max(1, int(rate))
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._cancelled = False

    def wait(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + timeout if timeout else float("inf")
        while True:
            with self._lock:
                if self._cancelled:
                    return False
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            time.sleep(0.05)
            if time.monotonic() >= deadline:
                return False

    def cancel(self):
        with self._lock:
            self._cancelled = True


@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_backoff: float = 0.25
    max_backoff: float = 3.0


class Client:
    def __init__(
        self,
        timeout: float = 20.0,
        limiter: Optional[TokenBucketLimiter] = None,
        retry: RetryConfig | None = None,
        headers: dict[str, str] | None = None,
    ):
        self._retry = retry or RetryConfig()
        self._limiter = limiter
        self._http = httpx.Client(
            timeout=timeout,
            headers=headers or {},
            follow_redirects=True,
        )

    @property
    def cookies(self):
        return self._http.cookies

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> Optional[httpx.Response]:
        return self._do("GET", url, params=params, headers=headers)

    def post(self, url: str, data: dict | None = None, json_data: dict | None = None, headers: dict | None = None) -> Optional[httpx.Response]:
        return self._do("POST", url, data=data, json_data=json_data, headers=headers)

    def _do(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        data: dict | None = None,
        json_data: dict | None = None,
        headers: dict | None = None,
    ) -> Optional[httpx.Response]:
        can_retry = method.upper() in ("GET", "HEAD", "OPTIONS", "DELETE")
        last_err: Optional[Exception] = None

        for attempt in range(self._retry.max_attempts):
            if self._limiter:
                if not self._limiter.wait(timeout=30):
                    return None

            try:
                merged_headers = {**self._http.headers, **(headers or {})}
                resp = self._http.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    json=json_data,
                    headers=merged_headers,
                )
                resp.raise_for_status()
                return resp
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last_err = e
                if not can_retry or attempt >= self._retry.max_attempts - 1:
                    return None
                backoff = min(self._retry.base_backoff * (2 ** attempt), self._retry.max_backoff)
                jitter = random.uniform(0, backoff * 0.5)
                time.sleep(backoff + jitter)
            except httpx.HTTPStatusError as e:
                last_err = e
                if not can_retry or attempt >= self._retry.max_attempts - 1:
                    return None
                if e.response.status_code >= 500 or e.response.status_code == 429:
                    backoff = min(self._retry.base_backoff * (2 ** attempt), self._retry.max_backoff)
                    jitter = random.uniform(0, backoff * 0.5)
                    time.sleep(backoff + jitter)
                else:
                    return None
            except Exception as e:
                last_err = e
                return None

        return None

    def close(self):
        self._http.close()
