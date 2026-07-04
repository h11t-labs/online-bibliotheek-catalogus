"""Tests for the polite HTTP client (obc.client) — no real network.

RateLimiter spacing is checked with real (short) monotonic timing; the retry and
404 paths use an httpx.MockTransport injected via Client(transport=...)."""

from __future__ import annotations

import time

import httpx

from obc import client


def test_rate_limiter_spaces_successive_calls():
    # per_second=50 -> ~20ms minimum interval. First wait() returns immediately and
    # arms the next slot; the second must block ~20ms. Loose bound to avoid flakes.
    rl = client.RateLimiter(per_second=50)
    start = time.monotonic()
    rl.wait()
    rl.wait()
    assert time.monotonic() - start >= 0.015


def test_get_retries_5xx_then_succeeds(monkeypatch):
    # No real sleeping: neutralise both tenacity's backoff and the rate limiter.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text="ok")

    c = client.Client(per_second=1000, cache=False,
                      transport=httpx.MockTransport(handler))
    r = c._get("https://example.test/x")
    c.close()
    assert r.status_code == 200
    assert calls["n"] == 3  # 500, 500, 200


def test_get_detail_html_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    c = client.Client(per_second=1000, cache=False,
                      transport=httpx.MockTransport(handler))
    assert c.get_detail_html("123", "een-boek") is None
    c.close()
