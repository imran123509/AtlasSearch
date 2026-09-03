"""Kafka emission.

`pages.fetched` carries a **blob pointer**, never the body. Message shape matches
features/KAFKA.md. Keyed by doc_id so all events for a document land on one
partition and fetch/parse/delete cannot race.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any

import structlog

from .models import BlobRef, FetchResult, Outcome

log = structlog.get_logger(__name__)


def doc_id(url: str) -> str:
    return "d:" + hashlib.blake2b(url.encode(), digest_size=8).hexdigest()


def fetched_event(
    result: FetchResult, blob: BlobRef | None, *, canonical_url: str | None = None
) -> dict[str, Any]:
    url = canonical_url or result.url
    return {
        "doc_id": doc_id(url),
        "url": url,
        "requested_url": result.url if canonical_url and canonical_url != result.url else None,
        "blob": blob.key if blob else None,
        "content_hash": result.content_hash,
        "fetched_at": result.fetched_at.isoformat(),
        "status": result.status,
        "outcome": str(result.outcome),
        "etag": result.etag,
        "last_modified": result.last_modified,
        "content_type": result.content_type,
        "size_bytes": blob.size_bytes if blob else 0,
        "elapsed_ms": round(result.elapsed_s * 1000),
        "redirect_target": result.redirect_target,
        "redirect_permanent": result.redirect_permanent,
    }


class Emitter(ABC):
    @abstractmethod
    async def emit(self, topic: str, key: str, value: dict[str, Any]) -> None: ...

    async def start(self) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None


class NullEmitter(Emitter):
    """Collects in memory. Used by tests and by `--dry-run`."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(self, topic: str, key: str, value: dict[str, Any]) -> None:
        self.messages.append((topic, key, value))


class KafkaEmitter(Emitter):
    def __init__(self, brokers: str) -> None:
        self.brokers = brokers
        self._producer = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.brokers,
            compression_type="zstd",  # text compresses ~4:1
            linger_ms=50,  # batching matters far more than 50 ms of latency
            enable_idempotence=True,
            acks="all",
            value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode(),
            key_serializer=lambda k: k.encode(),
        )
        await self._producer.start()

    async def emit(self, topic: str, key: str, value: dict[str, Any]) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaEmitter.start() was not awaited")
        await self._producer.send_and_wait(topic, key=key, value=value)

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


def should_emit(outcome: Outcome) -> bool:
    """Which outcomes are worth telling the pipeline about.

    304s are emitted so the scheduler can lower this page's change rate, and
    tombstones so the index can drop the document. Our own rate limiter
    declining a fetch is not news.
    """
    return outcome in {
        Outcome.FETCHED,
        Outcome.NOT_MODIFIED,
        Outcome.GONE,
        Outcome.REDIRECTED,
    }
