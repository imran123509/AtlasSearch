"""Parse worker: consumes `pages.fetched`, emits `pages.parsed` + link topics.

Bodies are fetched from the blob store by the pointer on the message — Kafka
carries pointers, never page bodies (features/KAFKA.md). Offsets are committed
**after** the side effect, so a crash re-processes rather than silently dropping
documents.

Parse work is grouped by host where possible, because site-template learning
(the highest-quality boilerplate layer) needs several pages from one site.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import signal
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from . import metrics
from .boilerplate import TemplateLearner
from .config import Config
from .links import authority_edges, discovery_urls
from .models import ParsedDocument
from .parse import ParseRejected
from .pipeline import Parser

log = structlog.get_logger(__name__)


class BlobReader(ABC):
    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    async def close(self) -> None:  # pragma: no cover
        return None


class LocalBlobReader(BlobReader):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    async def get(self, key: str) -> bytes:
        data = (self.root / key).read_bytes()
        return gzip.decompress(data) if key.endswith(".gz") else data


class S3BlobReader(BlobReader):  # pragma: no cover - needs a live endpoint
    def __init__(self, *, endpoint: str, bucket: str) -> None:
        self.endpoint, self.bucket = endpoint, bucket
        self._client = None

    async def _get_client(self):  # noqa: ANN202
        if self._client is None:
            import aioboto3

            self._cm = aioboto3.Session().client("s3", endpoint_url=self.endpoint)
            self._client = await self._cm.__aenter__()
        return self._client

    async def get(self, key: str) -> bytes:
        client = await self._get_client()
        obj = await client.get_object(Bucket=self.bucket, Key=key)
        body = await obj["Body"].read()
        return gzip.decompress(body) if key.endswith(".gz") else body

    async def close(self) -> None:
        if self._client is not None:
            await self._cm.__aexit__(None, None, None)


class Emitter(ABC):
    @abstractmethod
    async def emit(self, topic: str, key: str, value: dict) -> None: ...


class NullEmitter(Emitter):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict]] = []

    async def emit(self, topic: str, key: str, value: dict) -> None:
        self.messages.append((topic, key, value))

    def by_topic(self, topic: str) -> list[dict]:
        return [v for t, _, v in self.messages if t == topic]


class ParseWorker:
    def __init__(
        self,
        *,
        config: Config,
        parser: Parser,
        blobs: BlobReader,
        emitter: Emitter,
    ) -> None:
        self.cfg = config
        self.parser = parser
        self.blobs = blobs
        self.emitter = emitter
        self._stopping = asyncio.Event()

    async def handle(self, event: dict[str, Any]) -> ParsedDocument | None:
        """Process one `pages.fetched` message."""
        url = event.get("url")
        blob_key = event.get("blob")

        # 304s and tombstones carry no body; they are the scheduler's business.
        if not blob_key or not url:
            return None

        try:
            raw = await self.blobs.get(blob_key)
        except Exception:  # noqa: BLE001
            log.exception("parse.blob_unavailable", url=url, blob=blob_key)
            metrics.REJECTED.labels("blob_unavailable").inc()
            raise  # do NOT commit the offset — this is retryable

        fetched_at = event.get("fetched_at")
        try:
            doc = self.parser.parse(
                raw,
                url=url,
                content_type=event.get("content_type"),
                fetched_at=datetime.fromisoformat(fetched_at) if fetched_at else None,
            )
        except ParseRejected as exc:
            log.info("parse.rejected", url=url, reason=exc.reason)
            metrics.REJECTED.labels(exc.reason.split(":")[0]).inc()
            return None
        except Exception:  # noqa: BLE001 - one bad page must not kill the worker
            log.exception("parse.failed", url=url)
            metrics.REJECTED.labels("exception").inc()
            return None

        metrics.observe(doc)
        await self._publish(doc)
        return doc

    async def _publish(self, doc: ParsedDocument) -> None:
        if doc.is_indexable:
            await self.emitter.emit(self.cfg.topic_out, doc.doc_id, doc.to_event())
        else:
            # `noindex` — we parsed it, we may not index it. Its links are still
            # useful for discovery unless `nofollow` was also set.
            log.info("parse.noindex", url=doc.url, directives=sorted(doc.robots_meta))

        if "nofollow" in doc.robots_meta:
            return

        for link in authority_edges(doc.links):
            await self.emitter.emit(
                self.cfg.topic_links,
                link.target_url,
                {
                    "source_url": link.source_url,
                    "target_url": link.target_url,
                    "anchor_text": link.anchor_text,
                    "context": link.context,
                    "internal": link.internal,
                    "in_main_content": link.in_main_content,
                },
            )

        for url in discovery_urls(doc.links):
            await self.emitter.emit(
                self.cfg.topic_discovered, url, {"url": url, "source_url": doc.url}
            )

    async def run(self) -> None:  # pragma: no cover - needs a live broker
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
        import json

        consumer = AIOKafkaConsumer(
            self.cfg.topic_in,
            bootstrap_servers=self.cfg.kafka_brokers,
            group_id=self.cfg.consumer_group,
            enable_auto_commit=False,        # commit AFTER the side effect
            auto_offset_reset="earliest",
            max_poll_interval_ms=600_000,    # parsing is slow; do not get kicked
            value_deserializer=lambda v: json.loads(v.decode()),
        )
        await consumer.start()
        log.info("indexer.start", topic=self.cfg.topic_in, group=self.cfg.consumer_group)
        try:
            while not self._stopping.is_set():
                batch = await consumer.getmany(timeout_ms=1000, max_records=200)
                for _tp, messages in batch.items():
                    # Group by host so template learning has neighbours to compare.
                    for message in sorted(
                        messages, key=lambda m: (m.value or {}).get("url", "")
                    ):
                        await self.handle(message.value)
                if batch:
                    await consumer.commit()
        finally:
            await consumer.stop()

    def stop(self) -> None:
        self._stopping.set()


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-indexer")
    parser.add_argument("--blob-dir", default=None, help="Read blobs from disk instead of S3")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--metrics-port", type=int, default=0)
    parser.add_argument("--file", action="append", default=[], help="Parse a local HTML file")
    parser.add_argument("--url", default="https://example.com/", help="URL for --file")
    args = parser.parse_args(argv)

    structlog.configure(processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])

    cfg = Config()
    doc_parser = Parser(cfg, templates=TemplateLearner(cfg.boilerplate))

    # One-shot mode: useful for eyeballing extraction on a real page.
    if args.file:
        for path in args.file:
            raw = Path(path).read_bytes()
            doc = doc_parser.try_parse(raw, url=args.url)
            if doc is None:
                print(f"{path}: rejected")
                continue
            print(f"--- {path} ---")
            print(f"title    : {doc.title}")
            print(f"lang     : {doc.language.code} ({doc.language.source})")
            print(f"charset  : {doc.charset.encoding} (via {doc.charset.source})")
            print(f"render   : {doc.render}")
            print(f"retained : {doc.retained_chars}/{doc.visible_chars} "
                  f"({doc.retained_ratio:.1%}), blocks {doc.blocks_kept}/{doc.blocks_total}")
            print(f"links    : {len(doc.links)}")
            print(f"warnings : {doc.warnings}")
            print(f"body     : {doc.body[:400]}...")
        return 0

    if args.metrics_port:
        from prometheus_client import start_http_server

        start_http_server(args.metrics_port)

    blobs: BlobReader = (
        LocalBlobReader(args.blob_dir) if args.blob_dir
        else S3BlobReader(endpoint=cfg.s3_endpoint, bucket=cfg.s3_bucket)
    )
    emitter: Emitter = NullEmitter() if args.dry_run else _kafka_emitter(cfg)

    worker = ParseWorker(config=cfg, parser=doc_parser, blobs=blobs, emitter=emitter)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.run()
    finally:
        await blobs.close()
    return 0


def _kafka_emitter(cfg: Config) -> Emitter:  # pragma: no cover
    import json

    from aiokafka import AIOKafkaProducer

    class _Kafka(Emitter):
        def __init__(self) -> None:
            self.producer = AIOKafkaProducer(
                bootstrap_servers=cfg.kafka_brokers,
                compression_type="zstd",
                linger_ms=50,
                enable_idempotence=True,
                acks="all",
                value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode(),
                key_serializer=lambda k: k.encode(),
            )

        async def emit(self, topic: str, key: str, value: dict) -> None:
            await self.producer.send_and_wait(topic, key=key, value=value)

    return _Kafka()


def run() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    run()
