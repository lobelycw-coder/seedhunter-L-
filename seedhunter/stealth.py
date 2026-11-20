"""Fingerprint rotation."""
from __future__ import annotations

import random

UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]


def headers() -> dict[str, str]:
    return {
        "content-type": "application/json",
        "user-agent": random.choice(UAS),
        "accept": "application/json",
        "accept-encoding": "gzip, deflate, br",
    }


def jitter(ms: int) -> float:
    return random.uniform(0, ms) / 1000.0