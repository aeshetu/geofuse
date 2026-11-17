"""Source extractors. Each module exposes NAME, fetch() -> RawPage iterator, and parse(page) -> Features.

Fetching and parsing are separate on purpose: raw pages are landed in object storage exactly as
received, so any run can be re-parsed later (replay) without calling the upstream service again.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RawPage:
    source: str
    seq: int
    url: str
    content_type: str
    body: bytes

    @property
    def extension(self) -> str:
        return {"application/json": "json", "application/xml": "xml"}.get(self.content_type, "txt")


class SourceError(RuntimeError):
    """The upstream service answered, but with an error (often inside an HTTP 200)."""


def registry() -> dict:
    from geofuse.sources import ghcn_s3, mrds_wfs, usgs_quakes, wfigs_fires

    return {m.NAME: m for m in (usgs_quakes, wfigs_fires, mrds_wfs, ghcn_s3)}
