from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from geofuse.config import BBox

FIXTURES = Path(__file__).parent / "fixtures"
NORCAL = BBox.parse("-124.5,37.0,-119.5,42.0")
CONUS = BBox.parse("-125.0,24.0,-66.0,50.0")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(self, url: str, body: bytes, status_code: int = 200):
        self.url, self.content, self.status_code = url, body, status_code

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Serves queued bodies in order and records every request's params."""

    def __init__(self, bodies: list[bytes | tuple[int, bytes]]):
        self.bodies, self.calls = list(bodies), []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        item = self.bodies.pop(0)
        status, body = item if isinstance(item, tuple) else (200, item)
        return FakeResponse(f"{url}?{urlencode(params or {})}", body, status)


@pytest.fixture
def aoi() -> BBox:
    return NORCAL
