from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
# Canonicalisation is shared with the crawler; see links.py for why.
sys.path.insert(0, str(_ROOT.parent / "crawler" / "src"))

from atlas_indexer.config import Config  # noqa: E402
from atlas_indexer.pipeline import Parser  # noqa: E402


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def parser(cfg):
    return Parser(cfg)


ARTICLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Block-Max Indexes Explained</title>
  <meta name="description" content="How block upper bounds make retrieval sublinear.">
  <link rel="canonical" href="https://example.com/papers/bmw">
  <script type="application/ld+json">
  {"@type":"Article","datePublished":"2026-03-14","headline":"Block-Max Indexes"}
  </script>
</head>
<body>
  <header class="site-header"><a href="/">Home</a> <a href="/about">About</a></header>
  <nav class="main-nav">
    <a href="/a">Alpha</a> <a href="/b">Beta</a> <a href="/c">Gamma</a> <a href="/d">Delta</a>
  </nav>
  <main>
    <article>
      <h1>Block-Max Indexes Explained</h1>
      <p>The retrieval loop keeps a running threshold equal to the score of the current
         kth best result, and for each candidate block alignment it sums the per-term
         block maxima before deciding whether to decode anything at all.</p>
      <p>If that sum cannot exceed the threshold, the entire block is skipped without
         decoding a single posting, which is what makes retrieval sublinear in the
         length of the posting list rather than linear as a naive scan would be.</p>
      <h2>Why the feedback matters</h2>
      <p>Every good document found raises the threshold, which prunes harder, which is
         precisely why retrieving the top ten documents is dramatically cheaper than
         retrieving the top one thousand from the very same index structure.</p>
      <p>See the <a href="https://other.test/paper.pdf" rel="nofollow">original paper</a>
         for the full derivation and the experimental results on real corpora.</p>
    </article>
  </main>
  <aside class="sidebar related">
    <a href="/r1">Related one</a> <a href="/r2">Related two</a> <a href="/r3">Related three</a>
  </aside>
  <footer class="site-footer">
    <a href="/privacy">Privacy</a> <a href="/terms">Terms</a> &copy; 2026 Example Inc
  </footer>
</body>
</html>"""


@pytest.fixture
def article() -> bytes:
    return ARTICLE_HTML.encode("utf-8")
