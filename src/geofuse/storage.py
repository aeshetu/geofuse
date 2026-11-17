"""Object storage for the data lake: S3 (AWS, or any S3-compatible endpoint) or a local folder.

Layout, partitioned by source and date so each run is immutable and replayable:
    raw/<source>/dt=YYYY-MM-DD/run=<run_id>/page-0001.<ext>.gz   + manifest.json
    curated/dt=YYYY-MM-DD/run=<run_id>/features.jsonl.gz
    quarantine/dt=YYYY-MM-DD/run=<run_id>/rejects.jsonl.gz
    runs/<run_id>.json                                            (run report)
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> list[str]: ...


class LocalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def list(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        keys = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix))


class S3Store:
    def __init__(self, bucket: str, prefix: str = "", client=None, endpoint_url: str | None = None,
                 create: bool = False):
        import boto3

        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.client = client or boto3.client("s3", endpoint_url=endpoint_url)
        if create:
            try:
                self.client.head_bucket(Bucket=bucket)
            except self.client.exceptions.ClientError:
                self.client.create_bucket(Bucket=bucket)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def list(self, prefix: str) -> list[str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        keys = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self._key(prefix)):
            keys += [o["Key"][strip:] for o in page.get("Contents", [])]
        return sorted(keys)


def open_store(url: str, endpoint_url: str | None = None) -> ObjectStore:
    if url.startswith("s3://"):
        bucket, _, prefix = url.removeprefix("s3://").partition("/")
        # Creating the bucket is only for local S3 emulators; real buckets are provisioned separately.
        return S3Store(bucket, prefix, endpoint_url=endpoint_url, create=endpoint_url is not None)
    if url.startswith("file://"):
        return LocalStore(url.removeprefix("file://"))
    raise ValueError(f"store URL must start with s3:// or file://, got {url!r}")


def put_json(store: ObjectStore, key: str, obj) -> None:
    store.put(key, json.dumps(obj, indent=2, default=str).encode(), "application/json")


def get_json(store: ObjectStore, key: str):
    return json.loads(store.get(key))


def put_jsonl_gz(store: ObjectStore, key: str, records) -> int:
    lines = [json.dumps(r, default=str) for r in records]
    store.put(key, gzip.compress("\n".join(lines).encode()), "application/gzip")
    return len(lines)


def get_jsonl_gz(store: ObjectStore, key: str) -> list[dict]:
    text = gzip.decompress(store.get(key)).decode()
    return [json.loads(line) for line in text.splitlines() if line]
