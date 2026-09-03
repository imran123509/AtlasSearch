from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from atlas_indexer.config import Config
from atlas_indexer.main import BlobReader, LocalBlobReader, NullEmitter, ParseWorker
from atlas_indexer.pipeline import Parser

URL = "https://example.com/papers/bmw.html"


class ExplodingBlobReader(BlobReader):
    async def get(self, key: str) -> bytes:
        raise ConnectionError("blob store unreachable")


@pytest.fixture
def blobs(tmp_path, article):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "page.gz").write_bytes(gzip.compress(article))
    return LocalBlobReader(tmp_path)


@pytest.fixture
def worker(cfg, blobs):
    return ParseWorker(config=cfg, parser=Parser(cfg), blobs=blobs, emitter=NullEmitter())


def _event(**kw) -> dict:
    return {"doc_id": "d:1", "url": URL, "blob": "a/page.gz",
            "content_type": "text/html; charset=utf-8", **kw}


class TestEmission:
    async def test_parsed_document_emitted(self, worker):
        doc = await worker.handle(_event())
        assert doc is not None
        parsed = worker.emitter.by_topic("pages.parsed")
        assert len(parsed) == 1
        assert parsed[0]["title"] == "Block-Max Indexes Explained"
        assert "running threshold" in parsed[0]["body"]

    async def test_links_emitted_keyed_for_the_anchor_shuffle(self, worker):
        await worker.handle(_event())
        links = worker.emitter.by_topic("links.extracted")
        assert links
        assert all({"source_url", "target_url", "anchor_text"} <= set(link) for link in links)

    async def test_discovery_urls_emitted(self, worker):
        await worker.handle(_event())
        discovered = worker.emitter.by_topic("urls.discovered")
        assert discovered
        assert all(d["source_url"] == URL for d in discovered)

    async def test_nofollow_targets_are_discovered_but_carry_no_authority(self, worker):
        await worker.handle(_event())
        discovered = {d["url"] for d in worker.emitter.by_topic("urls.discovered")}
        authority = {link["target_url"] for link in worker.emitter.by_topic("links.extracted")}
        # The paper link is rel=nofollow: crawl it, do not vouch for it.
        assert "https://other.test/paper.pdf" in discovered
        assert "https://other.test/paper.pdf" not in authority


class TestRobotsDirectives:
    async def test_noindex_suppresses_the_document_but_not_discovery(self, cfg, tmp_path):
        html = (b'<html><head><meta name="robots" content="noindex"></head><body>'
                b'<article><p>Body text long enough to survive extraction here.</p>'
                b'<a href="/next">next page</a></article></body></html>')
        (tmp_path / "n.gz").write_bytes(gzip.compress(html))
        w = ParseWorker(config=cfg, parser=Parser(cfg),
                        blobs=LocalBlobReader(tmp_path), emitter=NullEmitter())

        await w.handle(_event(blob="n.gz"))
        assert w.emitter.by_topic("pages.parsed") == []
        assert w.emitter.by_topic("urls.discovered"), "noindex should not stop discovery"

    async def test_nofollow_meta_suppresses_all_link_emission(self, cfg, tmp_path):
        html = (b'<html><head><meta name="robots" content="nofollow"></head><body>'
                b'<article><p>Body text long enough to survive extraction here.</p>'
                b'<a href="/next">next page</a></article></body></html>')
        (tmp_path / "nf.gz").write_bytes(gzip.compress(html))
        w = ParseWorker(config=cfg, parser=Parser(cfg),
                        blobs=LocalBlobReader(tmp_path), emitter=NullEmitter())

        await w.handle(_event(blob="nf.gz"))
        assert w.emitter.by_topic("pages.parsed")
        assert w.emitter.by_topic("urls.discovered") == []
        assert w.emitter.by_topic("links.extracted") == []


class TestMessageHandling:
    async def test_304_message_without_a_blob_is_skipped(self, worker):
        assert await worker.handle(_event(blob=None, outcome="not_modified")) is None
        assert worker.emitter.messages == []

    async def test_tombstone_without_a_blob_is_skipped(self, worker):
        assert await worker.handle({"doc_id": "d:1", "url": URL, "blob": None}) is None

    async def test_message_without_a_url_is_skipped(self, worker):
        assert await worker.handle({"doc_id": "d:1", "blob": "a/page.gz"}) is None

    async def test_blob_unavailable_raises_so_the_offset_is_not_committed(self, cfg):
        """A missing blob is retryable. Swallowing it would silently drop the
        document, because the consumer would then commit past the message."""
        w = ParseWorker(config=cfg, parser=Parser(cfg),
                        blobs=ExplodingBlobReader(), emitter=NullEmitter())
        with pytest.raises(ConnectionError):
            await w.handle(_event())

    async def test_unparseable_body_is_dropped_without_raising(self, cfg, tmp_path):
        """A rejected document is an expected outcome on the open web, not an
        exception worth killing a worker over."""
        (tmp_path / "empty.gz").write_bytes(gzip.compress(b"   "))
        w = ParseWorker(config=cfg, parser=Parser(cfg),
                        blobs=LocalBlobReader(tmp_path), emitter=NullEmitter())
        assert await w.handle(_event(blob="empty.gz")) is None
        assert w.emitter.messages == []

    async def test_fetched_at_is_carried_through(self, worker):
        doc = await worker.handle(_event(fetched_at="2026-09-02T14:22:01+00:00"))
        assert doc.fetched_at.isoformat() == "2026-09-02T14:22:01+00:00"


class TestBlobReader:
    async def test_gz_blobs_are_decompressed(self, tmp_path):
        (tmp_path / "x.gz").write_bytes(gzip.compress(b"<html>hi</html>"))
        assert await LocalBlobReader(tmp_path).get("x.gz") == b"<html>hi</html>"

    async def test_plain_blobs_pass_through(self, tmp_path):
        (tmp_path / "x.html").write_bytes(b"<html>hi</html>")
        assert await LocalBlobReader(tmp_path).get("x.html") == b"<html>hi</html>"
