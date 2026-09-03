"""Blob store for raw fetched bodies.

Bodies go here; only a *pointer* goes on Kafka. At target volume, putting bodies
on the topic would mean 36 TB/day through the brokers, replicated 3× — Kafka is a
log, not a blob store. See features/KAFKA.md.

Keys are content-addressed, so identical bodies (mirrors, CDN copies, the ~⅓ of
the web that is duplicate) are stored once.
"""

from __future__ import annotations

import gzip
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .models import BlobRef, FetchResult

log = structlog.get_logger(__name__)


def blob_key(result: FetchResult) -> str:
    """`2026/09/02/<sha256[:2]>/<sha256>.gz` — date-partitioned, content-addressed."""
    digest = (result.content_hash or "").removeprefix("sha256:")
    d: datetime = result.fetched_at.astimezone(timezone.utc)
    return f"{d:%Y/%m/%d}/{digest[:2]}/{digest}.gz"


class BlobStore(ABC):
    @abstractmethod
    async def put(self, result: FetchResult) -> BlobRef: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class LocalBlobStore(BlobStore):
    """Filesystem-backed. For dev and tests; same key layout as S3."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, result: FetchResult) -> BlobRef:
        assert result.body is not None and result.content_hash is not None
        key = blob_key(result)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = gzip.compress(result.body)
        if not path.exists():
            path.write_bytes(payload)
        return BlobRef(key=key, size_bytes=len(result.body), content_hash=result.content_hash)

    async def exists(self, key: str) -> bool:
        return (self.root / key).exists()


class S3BlobStore(BlobStore):
    """S3 / MinIO.

    NOTE: this writes one object per page. At Build volume that is fine; the
    Target batches ~500 MB objects with an offset index, because 400M PUTs/day
    makes per-request cost and metadata overhead dominate. See features/STORAGE.md.
    """

    def __init__(self, *, endpoint: str, bucket: str, session=None) -> None:  # noqa: ANN001
        self.endpoint = endpoint
        self.bucket = bucket
        self._session = session
        self._client = None

    async def _get_client(self):  # noqa: ANN202
        if self._client is None:
            import aioboto3

            session = self._session or aioboto3.Session()
            self._cm = session.client("s3", endpoint_url=self.endpoint)
            self._client = await self._cm.__aenter__()
        return self._client

    async def put(self, result: FetchResult) -> BlobRef:
        assert result.body is not None and result.content_hash is not None
        key = blob_key(result)
        client = await self._get_client()

        # Content-addressed: if it is already there, the bytes are identical.
        if not await self.exists(key):
            await client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=gzip.compress(result.body),
                ContentEncoding="gzip",
                ContentType=result.content_type or "application/octet-stream",
                Metadata={
                    "source-url": result.url[:1024],
                    "fetched-at": result.fetched_at.isoformat(),
                    "status": str(result.status or 0),
                },
            )
        return BlobRef(key=key, size_bytes=len(result.body), content_hash=result.content_hash)

    async def exists(self, key: str) -> bool:
        client = await self._get_client()
        try:
            await client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - botocore raises ClientError for 404
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._cm.__aexit__(None, None, None)
            self._client = None
